from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tracking", "0008_merge_0005_userlinkedaccount_0007_remove_tguser_limits_json"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedule",
            name="config_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
