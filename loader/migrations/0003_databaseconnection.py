from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("loader", "0002_enginesetting")]

    operations = [
        migrations.CreateModel(
            name="DatabaseConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("kind", models.CharField(max_length=16)),
                ("host", models.CharField(blank=True, max_length=255)),
                ("port", models.PositiveIntegerField(blank=True, null=True)),
                ("database", models.CharField(blank=True, max_length=1024)),
                ("username", models.CharField(blank=True, max_length=255)),
                ("password_enc", models.TextField(blank=True)),
                ("options", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
    ]
