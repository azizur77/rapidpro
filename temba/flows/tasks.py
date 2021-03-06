
import logging
import time
from datetime import timedelta

import iso8601

from django.conf import settings
from django.utils import timezone
from django.utils.timesince import timesince

from celery.task import task

from temba.msgs.models import BROADCAST_BATCH, HANDLE_EVENT_TASK, TIMEOUT_EVENT, Broadcast, Msg
from temba.orgs.models import Org
from temba.utils.cache import QueueRecord
from temba.utils.dates import datetime_to_epoch
from temba.utils.queues import Queue, complete_task, nonoverlapping_task, push_task, start_task

from .models import (
    FLOW_BATCH,
    ExportFlowResultsTask,
    Flow,
    FlowCategoryCount,
    FlowNodeCount,
    FlowPathCount,
    FlowPathRecentRun,
    FlowRun,
    FlowRunCount,
    FlowSession,
    FlowStart,
    FlowStartCount,
)

FLOW_TIMEOUT_KEY = "flow_timeouts_%y_%m_%d"
logger = logging.getLogger(__name__)


@task(track_started=True, name="send_email_action_task")
def send_email_action_task(org_id, recipients, subject, message):
    org = Org.objects.filter(pk=org_id, is_active=True).first()
    if org:
        org.email_action_send(recipients, subject, message)


@task(track_started=True, name="update_run_expirations_task")
def update_run_expirations_task(flow_id):
    """
    Update all of our current run expirations according to our new expiration period
    """
    for run in FlowRun.objects.filter(flow_id=flow_id, is_active=True):
        if run.path:
            last_arrived_on = iso8601.parse_date(run.path[-1]["arrived_on"])
            run.update_expiration(last_arrived_on)

    # force an expiration update
    check_flows_task.apply()


@nonoverlapping_task(track_started=True, name="check_flows_task", lock_key="check_flows")  # pragma: no cover
def check_flows_task():
    """
    See if any flow runs need to be expired
    """
    runs = FlowRun.objects.filter(
        is_active=True, expires_on__lte=timezone.now(), org__flow_server_enabled=False
    ).order_by("expires_on")
    FlowRun.bulk_exit(runs, FlowRun.EXIT_TYPE_EXPIRED)


@nonoverlapping_task(
    track_started=True, name="check_flow_timeouts_task", lock_key="check_flow_timeouts", lock_timeout=3600
)  # pragma: no cover
def check_flow_timeouts_task():
    """
    See if any flow runs have timed out
    """
    # find any runs that should have timed out
    runs = FlowRun.objects.filter(is_active=True, timeout_on__lte=timezone.now(), org__flow_server_enabled=False)
    runs = runs.only("id", "org", "timeout_on")

    queued_timeouts = QueueRecord("flow_timeouts", lambda r: "%d:%d" % (r.id, datetime_to_epoch(r.timeout_on)))

    for run in runs:
        # ignore any run which was locked by previous calls to this task
        if not queued_timeouts.is_queued(run):
            try:
                task_payload = dict(type=TIMEOUT_EVENT, run=run.id, timeout_on=run.timeout_on)
                push_task(run.org_id, Queue.HANDLER, HANDLE_EVENT_TASK, task_payload)

                queued_timeouts.set_queued([run])
            except Exception:  # pragma: no cover
                logger.error("Error queuing timeout task for run #%d" % run.id, exc_info=True)


@task(track_started=True, name="continue_parent_flows")  # pragma: no cover
def continue_parent_flows(run_ids):
    runs = FlowRun.objects.filter(pk__in=run_ids)
    FlowRun.continue_parent_flow_runs(runs)


@task(track_started=True, name="interrupt_flow_runs_task")
def interrupt_flow_runs_task(flow_id):
    runs = FlowRun.objects.filter(is_active=True, exit_type=None, flow_id=flow_id)
    FlowRun.bulk_exit(runs, FlowRun.EXIT_TYPE_INTERRUPTED)


@task(track_started=True, name="export_flow_results_task")
def export_flow_results_task(export_id):
    """
    Export a flow to a file and e-mail a link to the user
    """
    ExportFlowResultsTask.objects.select_related("org").get(id=export_id).perform()


@task(track_started=True, name="start_flow_task")
def start_flow_task(start_id):
    flow_start = FlowStart.objects.get(pk=start_id)
    flow_start.start()


@task(track_started=True, name="start_msg_flow_batch")
def start_msg_flow_batch_task():
    # pop off the next task
    org_id, task_obj = start_task(Flow.START_MSG_FLOW_BATCH)

    # it is possible that somehow we might get None back if more workers were started than tasks got added, bail if so
    if task_obj is None:  # pragma: needs cover
        return

    start = time.time()

    try:
        task_type = task_obj.get("task_type", FLOW_BATCH)

        if task_type == FLOW_BATCH:
            # instantiate all the objects we need that were serialized as JSON
            flow = Flow.objects.filter(pk=task_obj["flow"], is_active=True, is_archived=False).first()
            if not flow:  # pragma: needs cover
                return

            broadcasts = [] if not task_obj["broadcasts"] else Broadcast.objects.filter(pk__in=task_obj["broadcasts"])
            started_flows = [] if not task_obj["started_flows"] else task_obj["started_flows"]
            start_msg = None if not task_obj["start_msg"] else Msg.objects.filter(id=task_obj["start_msg"]).first()
            extra = task_obj["extra"]
            flow_start = (
                None if not task_obj["flow_start"] else FlowStart.objects.filter(pk=task_obj["flow_start"]).first()
            )
            contacts = task_obj["contacts"]

            # and go do our work
            flow.start_msg_flow_batch(
                contacts,
                broadcasts=broadcasts,
                started_flows=started_flows,
                start_msg=start_msg,
                extra=extra,
                flow_start=flow_start,
            )

            print(
                "Started batch of %d contacts in flow %d [%d] in %0.3fs"
                % (len(contacts), flow.id, flow.org_id, time.time() - start)
            )

        elif task_type == BROADCAST_BATCH:
            broadcast = Broadcast.objects.filter(org_id=org_id, id=task_obj["broadcast"]).first()
            if broadcast:
                broadcast.send_batch(**task_obj["kwargs"])

            print(
                "Sent batch of %d messages in broadcast [%d] in %0.3fs"
                % (len(task_obj["kwargs"]["urn_ids"]), broadcast.id, time.time() - start)
            )

    finally:
        complete_task(Flow.START_MSG_FLOW_BATCH, org_id)


@nonoverlapping_task(
    track_started=True, name="squash_flowpathcounts", lock_key="squash_flowpathcounts", lock_timeout=7200
)
def squash_flowpathcounts():
    FlowPathCount.squash()


@nonoverlapping_task(
    track_started=True, name="squash_flowruncounts", lock_key="squash_flowruncounts", lock_timeout=7200
)
def squash_flowruncounts():
    FlowNodeCount.squash()
    FlowRunCount.squash()
    FlowCategoryCount.squash()
    FlowPathRecentRun.prune()
    FlowStartCount.squash()


@nonoverlapping_task(track_started=True, name="trim_flow_sessions")
def trim_flow_sessions():
    """
    Cleanup old flow sessions
    """
    threshold = timezone.now() - timedelta(days=settings.FLOW_SESSION_TRIM_DAYS)
    num_deleted = 0
    start = timezone.now()

    print(f"Deleting flow sessions which ended before {threshold.isoformat()}...")

    while True:
        session_ids = list(FlowSession.objects.filter(ended_on__lte=threshold).values_list("id", flat=True)[:1000])
        if not session_ids:
            break

        # detach any flows runs that belong to these sessions
        FlowRun.objects.filter(session_id__in=session_ids).update(session_id=None)

        FlowSession.objects.filter(id__in=session_ids).delete()
        num_deleted += len(session_ids)

        if num_deleted % 10000 == 0:  # pragma: no cover
            print(f" > Deleted {num_deleted} flow sessions")

    elapsed = timesince(start)
    print(f"Deleted {num_deleted} flow sessions which ended before {threshold.isoformat()} in {elapsed}")
