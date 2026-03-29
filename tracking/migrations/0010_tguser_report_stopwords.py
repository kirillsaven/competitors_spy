from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tracking", "0009_schedule_config_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="tguser",
            name="report_stopwords",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
