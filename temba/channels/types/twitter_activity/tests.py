
from mock import patch

from django.test import override_settings
from django.urls import reverse

from temba.contacts.models import URN
from temba.tests import TembaTest
from temba.utils.twitter import TwythonError

from ...models import Channel
from .tasks import resolve_twitter_ids


class TwitterActivityTypeTest(TembaTest):
    def setUp(self):
        super().setUp()

        self.channel = Channel.create(
            self.org,
            self.user,
            None,
            "TWT",
            name="Twitter Beta",
            address="beta_bob",
            role="SR",
            config={
                "api_key": "ak1",
                "api_secret": "as1",
                "access_token": "at1",
                "access_token_secret": "ats1",
                "handle_id": "h123456",
                "webhook_id": "1234567",
                "env_name": "beta",
            },
        )

    @override_settings(IS_PROD=True)
    @patch("temba.utils.twitter.TembaTwython.get_webhooks")
    @patch("temba.utils.twitter.TembaTwython.delete_webhook")
    @patch("temba.utils.twitter.TembaTwython.subscribe_to_webhook")
    @patch("temba.utils.twitter.TembaTwython.register_webhook")
    @patch("twython.Twython.verify_credentials")
    def test_claim(
        self,
        mock_verify_credentials,
        mock_register_webhook,
        mock_subscribe_to_webhook,
        mock_delete_webhook,
        mock_get_webhooks,
    ):
        mock_get_webhooks.return_value = [{"id": "webhook_id"}]
        mock_delete_webhook.return_value = {"ok", True}

        url = reverse("channels.types.twitter_activity.claim")
        self.login(self.admin)

        response = self.client.get(reverse("channels.channel_claim"))
        self.assertContains(response, "/channels/types/twitter_activity/claim")

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect Twitter")

        self.assertEqual(
            list(response.context["form"].fields.keys()),
            ["api_key", "api_secret", "access_token", "access_token_secret", "env_name", "loc"],
        )

        # try submitting empty form
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, "form", "api_key", "This field is required.")
        self.assertFormError(response, "form", "api_secret", "This field is required.")
        self.assertFormError(response, "form", "access_token", "This field is required.")
        self.assertFormError(response, "form", "access_token_secret", "This field is required.")

        # try submitting with invalid credentials
        mock_verify_credentials.side_effect = TwythonError("Invalid credentials")

        response = self.client.post(
            url, {"api_key": "ak", "api_secret": "as", "access_token": "at", "access_token_secret": "ats"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, "form", None, "The provided Twitter credentials do not appear to be valid.")

        # error registering webhook
        mock_verify_credentials.return_value = {"id": "87654", "screen_name": "jimmy"}
        mock_verify_credentials.side_effect = None
        mock_register_webhook.side_effect = TwythonError("Exceeded number of webhooks")

        response = self.client.post(
            url,
            {
                "api_key": "ak",
                "api_secret": "as",
                "access_token": "at",
                "access_token_secret": "ats",
                "env_name": "production",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, "form", None, "Exceeded number of webhooks")

        # try a valid submission
        mock_register_webhook.side_effect = None
        mock_register_webhook.return_value = {"id": "1234567"}

        response = self.client.post(
            url,
            {
                "api_key": "ak",
                "api_secret": "as",
                "access_token": "at",
                "access_token_secret": "ats",
                "env_name": "beta",
            },
        )
        self.assertEqual(response.status_code, 302)

        channel = Channel.objects.get(address="jimmy", is_active=True)
        self.assertEqual(
            channel.config,
            {
                "handle_id": "87654",
                "api_key": "ak",
                "api_secret": "as",
                "access_token": "at",
                "env_name": "beta",
                "access_token_secret": "ats",
                "webhook_id": "1234567",
                "callback_domain": channel.callback_domain,
            },
        )
        self.assertTrue(channel.get_type().has_attachment_support(channel))

        mock_register_webhook.assert_called_with(
            "beta", "https://%s/c/twt/%s/receive" % (channel.callback_domain, channel.uuid)
        )
        mock_subscribe_to_webhook.assert_called_with("beta")

    @override_settings(IS_PROD=True)
    @patch("temba.utils.twitter.TembaTwython.delete_webhook")
    def test_release(self, mock_delete_webhook):
        self.channel.release()
        mock_delete_webhook.assert_called_once_with("beta", "1234567")

    @patch("twython.Twython.lookup_user")
    def test_resolve(self, mock_lookup_user):
        self.joe = self.create_contact("joe", twitter="therealjoe")

        urn = self.joe.get_urns()[0]

        # test no return value, should cause joe to be stopped
        mock_lookup_user.return_value = []
        resolve_twitter_ids()

        self.joe.refresh_from_db()
        urn.refresh_from_db()
        self.assertTrue(self.joe.is_stopped)
        self.assertIsNone(urn.display)
        self.assertEqual("twitter:therealjoe", urn.identity)
        self.assertEqual("therealjoe", urn.path)

        self.joe.unstop(self.admin)

        # test a real return value
        mock_lookup_user.return_value = [dict(screen_name="TheRealJoe", id="123456")]
        resolve_twitter_ids()

        urn.refresh_from_db()
        self.assertIsNone(urn.contact)

        new_urn = self.joe.get_urns()[0]
        self.assertEqual("twitterid:123456", new_urn.identity)
        self.assertEqual("123456", new_urn.path)
        self.assertEqual("therealjoe", new_urn.display)
        self.assertEqual("twitterid:123456#therealjoe", new_urn.urn)

        old_fred = self.create_contact("old fred", urn=URN.from_twitter("fred"))
        new_fred = self.create_contact("new fred", urn=URN.from_twitterid("12345", screen_name="fred"))

        mock_lookup_user.return_value = [dict(screen_name="fred", id="12345")]
        resolve_twitter_ids()

        # new fred shouldn't have any URNs anymore as he really is old_fred
        self.assertEqual(0, len(new_fred.urns.all()))

        # old fred should be unchanged
        self.assertEqual("twitterid:12345", old_fred.urns.all()[0].identity)

        self.jane = self.create_contact("jane", twitter="jane10")
        mock_lookup_user.side_effect = Exception(
            "Twitter API returned a 404 (Not Found), No user matches for specified terms."
        )
        resolve_twitter_ids()

        self.jane.refresh_from_db()
        self.assertTrue(self.jane.is_stopped)

        self.sarah = self.create_contact("sarah", twitter="sarah20")
        mock_lookup_user.side_effect = Exception("Unable to reach API")
        resolve_twitter_ids()

        self.sarah.refresh_from_db()
        self.assertFalse(self.sarah.is_stopped)
