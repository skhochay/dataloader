from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("loader", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="EngineSetting",
            fields=[
                ("key", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("value", models.JSONField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
