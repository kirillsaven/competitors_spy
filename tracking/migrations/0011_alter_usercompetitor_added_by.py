from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tracking", "0010_tguser_report_stopwords"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usercompetitor",
            name="added_by",
            field=models.CharField(
                choices=[("manual", "Manual"), ("auto", "Auto"), ("suggested", "Suggested")],
                default="manual",
                max_length=16,
            ),
        ),
    ]
