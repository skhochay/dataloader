from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

OLD_KEYS = {
    "STAGE_DIR": "stage_dir",
    "PROCESSING_DIR": "processing_dir",
    "ARCHIVE_DIR": "archive_dir",
    "FAIL_DIR": "fail_dir",
}


def create_default_profile(apps, schema_editor):
    """One 'Default' profile from the folders used so far (settings, plus any edit on the old Configuration page)."""
    Profile = apps.get_model("loader", "DirectoryProfile")
    Setting = apps.get_model("loader", "EngineSetting")
    Job = apps.get_model("loader", "LoadJob")
    values = dict(settings.DEFAULT_PROFILE)
    for old, new in OLD_KEYS.items():
        row = Setting.objects.filter(key=old).first()
        if row and isinstance(row.value, str):
            values[new] = row.value
    profile = Profile.objects.create(**values)
    Setting.objects.filter(key__in=list(OLD_KEYS)).delete()
    Job.objects.update(profile=profile, profile_name=profile.name)


class Migration(migrations.Migration):
    dependencies = [("loader", "0003_databaseconnection")]

    operations = [
        migrations.CreateModel(
            name="DirectoryProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("enabled", models.BooleanField(default=True)),
                ("stage_dir", models.CharField(max_length=1024)),
                ("processing_dir", models.CharField(max_length=1024)),
                ("archive_dir", models.CharField(max_length=1024)),
                ("fail_dir", models.CharField(max_length=1024)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="loadjob",
            name="profile",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="jobs", to="loader.directoryprofile"),
        ),
        migrations.AddField(
            model_name="loadjob",
            name="profile_name",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(create_default_profile, migrations.RunPython.noop),
    ]
