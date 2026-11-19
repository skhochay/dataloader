from django.db import models
from django.utils import timezone


class DirectoryProfile(models.Model):
    """A named set of four folders. The engine watches the stage folder of every enabled profile."""

    name = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    stage_dir = models.CharField(max_length=1024)
    processing_dir = models.CharField(max_length=1024)
    archive_dir = models.CharField(max_length=1024)
    fail_dir = models.CharField(max_length=1024)
    # How many files of this profile may load at once. 0 = no limit, 1 = one at a time, oldest first.
    max_parallel = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class LoadJob(models.Model):
    """One file in the work queue, and the record of loading it. Created by the engine, never by hand.

    QUEUED -> PROCESSING (a worker holds a lease) -> LOADED or FAILED. A worker that dies lets its
    lease expire and the job goes back to QUEUED. A transient error also goes back to QUEUED, with a delay.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Loading"
        LOADED = "loaded", "Loaded"
        FAILED = "failed", "Failed"
        REQUEUED = "requeued", "Queued for retry"

    profile = models.ForeignKey(DirectoryProfile, null=True, blank=True, related_name="jobs", on_delete=models.SET_NULL)
    profile_name = models.CharField(max_length=64, blank=True)  # kept if the profile is deleted
    filename = models.CharField(max_length=255)
    checksum = models.CharField(max_length=64, blank=True, db_index=True)
    size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING, db_index=True)
    rows_loaded = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    target = models.CharField(max_length=512, blank=True)  # where the rows go: "connection name -> table"; empty = DataLoader's own storage
    final_path = models.CharField(max_length=1024, blank=True)
    # Queue bookkeeping
    path = models.CharField(max_length=1024, blank=True)  # where the file is now (processing folder)
    archive_dir = models.CharField(max_length=1024, blank=True)  # copied from the profile when queued
    fail_dir = models.CharField(max_length=1024, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)  # retry delay: not claimable before this
    lease_until = models.DateTimeField(null=True, blank=True)  # a running job's lease; renewed by its worker
    worker = models.CharField(max_length=32, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.filename} ({self.status})"


class ChecksumClaim(models.Model):
    """Held while a file with this content is loading, so two workers never load identical files at once."""

    checksum = models.CharField(max_length=64, unique=True)
    job = models.OneToOneField(LoadJob, related_name="checksum_claim", on_delete=models.CASCADE)


class Record(models.Model):
    """A loaded row. Replace or extend this with your own typed target models."""

    job = models.ForeignKey(LoadJob, related_name="records", on_delete=models.CASCADE)
    row_number = models.PositiveIntegerField()
    data = models.JSONField()

    class Meta:
        indexes = [models.Index(fields=["job", "row_number"], name="record_job_row_idx")]


class EngineSetting(models.Model):
    """A setting changed on the Configuration page. Absent = use the default from settings.py."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key} = {self.value!r}"


class DatabaseConnection(models.Model):
    """A named connection to an external database. The password is stored encrypted."""

    name = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    database = models.CharField(max_length=1024, blank=True)  # database, service name, or file path
    username = models.CharField(max_length=255, blank=True)
    password_enc = models.TextField(blank=True)
    options = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.kind})"


class Mapping(models.Model):
    """Says where a directory profile's files are loaded: one database connection, and which file names go to which table."""

    profile = models.OneToOneField(DirectoryProfile, related_name="mapping", on_delete=models.CASCADE)
    connection = models.ForeignKey(DatabaseConnection, related_name="mappings", on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["profile__name"]

    def __str__(self):
        return f"{self.profile.name} -> {self.connection.name}"


class MappingRule(models.Model):
    """One line of a mapping: a file name pattern (emp*.*) and the table it loads into (dbo.EMP). First match wins."""

    mapping = models.ForeignKey(Mapping, related_name="rules", on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    pattern = models.CharField(max_length=255)
    table = models.CharField(max_length=400)
    create_table = models.BooleanField(default=False)  # create the table from the file's columns when it does not exist
    merge_keys = models.CharField(max_length=255, blank=True, default="")  # "id" or "a, b": update rows with the same key, add the rest (MERGE); empty = plain insert

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.pattern} => {self.table}"
