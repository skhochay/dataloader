import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="LoadJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=255)),
                ("checksum", models.CharField(blank=True, db_index=True, max_length=64)),
                ("size_bytes", models.BigIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("processing", "Loading"),
                            ("loaded", "Loaded"),
                            ("failed", "Failed"),
                            ("requeued", "Queued for retry"),
                        ],
                        db_index=True,
                        default="processing",
                        max_length=16,
                    ),
                ),
                ("rows_loaded", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True)),
                ("final_path", models.CharField(blank=True, max_length=1024)),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-started_at", "-id"]},
        ),
        migrations.CreateModel(
            name="Record",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("row_number", models.PositiveIntegerField()),
                ("data", models.JSONField()),
                (
                    "job",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="records", to="loader.loadjob"),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="record",
            index=models.Index(fields=["job", "row_number"], name="record_job_row_idx"),
        ),
    ]
