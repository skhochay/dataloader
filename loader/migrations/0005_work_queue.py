from django.db import migrations, models
import django.db.models.deletion



class Migration(migrations.Migration):
    dependencies = [("loader", "0004_directoryprofile")]

    operations = [
        migrations.AddField(model_name="directoryprofile", name="max_parallel", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="loadjob", name="path", field=models.CharField(blank=True, max_length=1024)),
        migrations.AddField(model_name="loadjob", name="archive_dir", field=models.CharField(blank=True, max_length=1024)),
        migrations.AddField(model_name="loadjob", name="fail_dir", field=models.CharField(blank=True, max_length=1024)),
        migrations.AddField(model_name="loadjob", name="attempts", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="loadjob", name="next_attempt_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="loadjob", name="lease_until", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="loadjob", name="worker", field=models.CharField(blank=True, max_length=32)),
        migrations.AlterField(
            model_name="loadjob",
            name="status",
            field=models.CharField(
                choices=[("queued", "Queued"), ("processing", "Loading"), ("loaded", "Loaded"), ("failed", "Failed"), ("requeued", "Queued for retry")],
                db_index=True, default="processing", max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="ChecksumClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("checksum", models.CharField(max_length=64, unique=True)),
                ("job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="checksum_claim", to="loader.loadjob")),
            ],
        ),
    ]
