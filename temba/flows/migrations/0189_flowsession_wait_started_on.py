# Generated by Django 2.1.3 on 2018-12-04 19:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("flows", "0188_auto_20181128_1226")]

    operations = [
        migrations.AddField(
            model_name="flowsession",
            name="wait_started_on",
            field=models.DateTimeField(help_text="When this session started waiting (if at all)", null=True),
        )
    ]
