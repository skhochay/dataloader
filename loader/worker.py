"""The work queue and the workers that drain it.

The scanner (engine.py) turns each ready file into a QUEUED LoadJob and moves the file into the
profile's processing folder. Any number of workers, in this process or in child processes, then:

    claim   one conditional UPDATE (QUEUED -> PROCESSING) that only one worker can win, so no
            database-specific locking is needed
    lease   a running job's lease_until is renewed every few seconds; a dead worker's lease runs out
            and the job goes back to QUEUED (requeue_expired)
    load    parse and insert in one transaction. The same transaction marks the job LOADED, and only
            if this worker still holds the lease, so a worker that lost its lease rolls back
    retry   temporary errors put the job back with a growing delay until MAX_ATTEMPTS are used;
            anything else, and the last attempt, sends the file to the fail folder
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.db import IntegrityError, InterfaceError, OperationalError, close_old_connections, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from . import targets
from .handlers import load_records
from .mapping import key_list, resolve_rule
from .models import ChecksumClaim, DirectoryProfile, LoadJob
from .parsers import PARSERS, ParseError

log = logging.getLogger("loader.worker")

LEASE_SECONDS = float(os.environ.get("DATALOADER_LEASE_SECONDS", "60"))  # how long a claim lasts without being renewed
RENEW_EVERY = max(LEASE_SECONDS / 6, 1.0)  # seconds between renewals (and worker status writes)
ERROR_SIDECAR = ".error.txt"
Status = LoadJob.Status


class DuplicateFileError(Exception):
    """Identical contents were already loaded."""


class LeaseLost(Exception):
    """This worker no longer owns the job (its lease expired and the job was given to someone else)."""


def sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backoff_seconds(attempts: int) -> float:
    """Wait before the next attempt: 5 s, 10 s, 20 s ... at most 5 minutes."""
    return min(5.0 * 2 ** max(attempts - 1, 0), 300.0)


def is_transient(exc: BaseException) -> bool:
    """Errors worth another try: the database or the file system hiccuped, the file itself is fine."""
    if isinstance(exc, (ParseError, DuplicateFileError, FileNotFoundError, LeaseLost, targets.TargetError)):
        return False
    return isinstance(exc, (OperationalError, InterfaceError, OSError, targets.TargetDatabaseError))


# ---- queue operations -------------------------------------------------------
def release(job: LoadJob, *, delay: float = 0.0, error: str = "", count_attempt: bool = True):
    """Put a claimed job back in the queue (only if it is still ours or nobody's)."""
    fields = dict(
        status=Status.QUEUED,
        lease_until=None,
        next_attempt_at=timezone.now() + timedelta(seconds=delay) if delay else None,
    )
    if error:
        fields["error"] = error
    if not count_attempt:
        fields["attempts"] = F("attempts") - 1
    ChecksumClaim.objects.filter(job_id=job.pk).delete()
    return LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=job.worker).update(**fields)


def _move(job: LoadJob, source: Path, directory: str) -> Path:
    stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    destination = Path(directory) / f"{stamp}_job{job.pk}_{job.filename}"
    shutil.move(str(source), str(destination))
    return destination


def settle(job: LoadJob, ok: bool, error: str = ""):
    """After the outcome is recorded: move the file to archive or fail, with a note for failures."""
    source = Path(job.path)
    directory = job.archive_dir if ok else job.fail_dir
    if not source.exists():
        log.warning("Job %s: %s is no longer in %s, so there is nothing to move", job.pk, source.name, source.parent)
        return
    if not directory:
        log.warning("Job %s has no %s folder; %s stays in %s", job.pk, "archive" if ok else "fail", source.name, source.parent)
        return
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        destination = _move(job, source, directory)
        if not ok:
            Path(str(destination) + ERROR_SIDECAR).write_text(error + "\n", encoding="utf-8")
        LoadJob.objects.filter(pk=job.pk).update(final_path=str(destination))
        job.final_path = str(destination)
    except OSError:
        log.exception("Could not move %s to %s; it stays in %s", source.name, directory, source.parent)


def fail(job: LoadJob, error: str, *, owner: str | None = None) -> bool:
    """Mark a job FAILED (if still ours) and move its file to the fail folder."""
    where = Q(pk=job.pk) & (Q(status=Status.PROCESSING, worker=owner) if owner is not None else Q(status__in=[Status.QUEUED, Status.PROCESSING]))
    updated = LoadJob.objects.filter(where).update(
        status=Status.FAILED, error=error, finished_at=timezone.now(), lease_until=None
    )
    ChecksumClaim.objects.filter(job_id=job.pk).delete()
    if updated:
        job.status, job.error = Status.FAILED, error
        settle(job, ok=False, error=error)
    return bool(updated)


def _give_back(job: LoadJob, max_attempts: int) -> bool:
    """A worker stopped without finishing this job: queue it again, or fail it if it has used its attempts."""
    if job.attempts >= max_attempts:
        return fail(job, f"Gave up after {job.attempts} attempts; the worker stopped before finishing.")
    changed = LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=job.worker, lease_until=job.lease_until).update(
        status=Status.QUEUED, lease_until=None, next_attempt_at=None,
        error="The worker stopped before this load finished; it was queued again.",
    )
    ChecksumClaim.objects.filter(job_id=job.pk).delete()
    if changed:
        log.warning("Job %s (%s) was queued again: worker %s stopped", job.pk, job.filename, job.worker)
    return bool(changed)


def requeue_expired(max_attempts: int) -> int:
    """Give back jobs whose worker stopped renewing the lease."""
    return sum(_give_back(job, max_attempts) for job in LoadJob.objects.filter(status=Status.PROCESSING, lease_until__lt=timezone.now()))


def requeue_worker(name: str, max_attempts: int) -> int:
    """A worker process is known to be gone: give back what it held, without waiting for its lease to run out."""
    return sum(_give_back(job, max_attempts) for job in LoadJob.objects.filter(status=Status.PROCESSING, worker=name))


def release_all_processing():
    """At engine start no worker of ours is running, so every PROCESSING job is a leftover."""
    for job in LoadJob.objects.filter(status=Status.PROCESSING):
        LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING).update(
            status=Status.QUEUED, lease_until=None, next_attempt_at=None,
            error="The engine stopped before this load finished; it was queued again.",
        )
        ChecksumClaim.objects.filter(job_id=job.pk).delete()


def queue_counts() -> dict:
    from django.db.models import Count

    rows = {r["status"]: r["n"] for r in LoadJob.objects.filter(status__in=[Status.QUEUED, Status.PROCESSING]).values("status").annotate(n=Count("id"))}
    return {"queued": rows.get(Status.QUEUED, 0), "processing": rows.get(Status.PROCESSING, 0)}


# ---- the worker ---------------------------------------------------------------
class Worker:
    """Claims jobs and loads them. Runs as a child process (run_worker) or inline in the engine."""

    def __init__(self, cfg_source, name: str, status_file: Path | None = None, stop=None):
        self._cfg_source = cfg_source  # callable returning the current EngineConfig
        self.name = name
        self.status_file = status_file
        self.stop = stop or threading.Event()
        self.current: LoadJob | None = None
        self._pulse_stop = threading.Event()
        self._pulse: threading.Thread | None = None

    @property
    def cfg(self):
        return self._cfg_source()

    # -- claiming --
    def claim_next(self) -> LoadJob | None:
        now = timezone.now()
        candidates = (
            LoadJob.objects.filter(status=Status.QUEUED)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("id")[:100]
        )
        for job in candidates:
            won = LoadJob.objects.filter(pk=job.pk, status=Status.QUEUED).update(
                status=Status.PROCESSING,
                worker=self.name,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                attempts=F("attempts") + 1,
            )
            if not won:
                continue  # another worker was faster
            job.refresh_from_db()
            if not self._allowed(job):
                release(job, delay=1.0, count_attempt=False)
                continue
            return job
        return None

    @staticmethod
    def _allowed(job: LoadJob) -> bool:
        """Honour the profile's max_parallel. Decided after claiming, so it is race-free:
        lower ids win, and a loser puts its claim back."""
        if job.profile_id is None:
            return True
        limit = DirectoryProfile.objects.filter(pk=job.profile_id).values_list("max_parallel", flat=True).first() or 0
        if not limit:
            return True
        running = list(LoadJob.objects.filter(profile_id=job.profile_id, status=Status.PROCESSING).order_by("id").values_list("id", flat=True))
        if job.pk not in running[:limit]:
            return False
        if limit == 1:  # one at a time means in order: nothing older may still be waiting or running
            return not LoadJob.objects.filter(profile_id=job.profile_id, status__in=[Status.QUEUED, Status.PROCESSING], pk__lt=job.pk).exists()
        return True

    def drain(self) -> int:
        """Process jobs until the queue has nothing claimable. Returns how many were handled."""
        handled = 0
        while not self.stop.is_set():
            close_old_connections()
            try:
                job = self.claim_next()
            except (OperationalError, InterfaceError):
                log.warning("Worker %s could not reach the database; will try again.", self.name, exc_info=True)
                connection.close()
                break
            if job is None:
                break
            self.process(job)
            handled += 1
        return handled

    # -- loading --
    def process(self, job: LoadJob):
        cfg = self.cfg
        self.current = job
        path = Path(job.path)
        log.info("[%s] %s loading %s (job %s, attempt %s)", job.profile_name, self.name, job.filename, job.pk, job.attempts)
        claim = None
        try:
            if not path.is_file():
                raise FileNotFoundError(f"the file is no longer at {path}")
            job.size_bytes = path.stat().st_size
            job.checksum = sha256_of(path)
            LoadJob.objects.filter(pk=job.pk).update(size_bytes=job.size_bytes, checksum=job.checksum)
            parser = PARSERS.get(path.suffix.lower())
            if parser is None:
                raise ParseError(f"unsupported file type '{path.suffix}'; supported: {', '.join(list(PARSERS))}")
            mapping, rule = resolve_rule(job.profile, job.filename) if job.profile_id else (None, None)
            if mapping is not None and rule is None:
                raise targets.NoMappingRuleError(
                    f"no rule in the mapping of profile “{job.profile_name}” matches the file name {job.filename}. "
                    "Add a rule for it on the Mapping screen."
                )
            label = f"{mapping.connection.name} → {rule.table}" if mapping is not None else ""
            if cfg.reject_duplicates:
                try:
                    with transaction.atomic():
                        claim = ChecksumClaim.objects.create(checksum=job.checksum, job=job)
                except IntegrityError:
                    # An identical file is loading right now. Wait for its outcome rather than racing it.
                    release(job, delay=max(cfg.poll_interval, 1.0), count_attempt=False)
                    log.info("[%s] %s has the same contents as a file loading now; waiting", job.profile_name, job.filename)
                    return
                # "Already loaded" means into the same place: the same file loaded earlier somewhere else doesn't count.
                if LoadJob.objects.filter(checksum=job.checksum, status=Status.LOADED, target=label).exclude(pk=job.pk).exists():
                    raise DuplicateFileError(
                        f"identical file contents were already loaded into {label or 'DataLoader’s own storage'}. "
                        "Change the file, or turn off “Reject duplicate files” on the Configuration page."
                    )
            if mapping is None:
                count = self._load_local(job, parser, path, cfg)
            else:
                count = self._load_mapped(job, parser, path, cfg, mapping, rule)
        except LeaseLost as exc:
            log.warning("[%s] %s: %s", job.profile_name, job.filename, exc)
        except Exception as exc:
            self._handle_error(job, exc, cfg)
        else:
            job.status, job.rows_loaded = Status.LOADED, count
            log.info("[%s] Loaded %s: %s rows", job.profile_name, job.filename, count)
            settle(job, ok=True)
        finally:
            if claim is not None:
                ChecksumClaim.objects.filter(pk=claim.pk).delete()
            self.current = None

    def _load_local(self, job, parser, path, cfg) -> int:
        """No mapping: rows go to DataLoader's own database, in one transaction with the LOADED mark."""
        rows = parser(path)
        try:
            with transaction.atomic():
                count = load_records(job, rows, cfg.batch_size)
                owned = LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=self.name).update(
                    status=Status.LOADED, rows_loaded=count, error="", finished_at=timezone.now(), lease_until=None
                )
                if not owned:
                    raise LeaseLost("the lease expired while loading, so this load was rolled back")
        finally:
            rows.close()  # release the file handle now; Windows can't move an open file
        return count

    def _load_mapped(self, job, parser, path, cfg, mapping, rule) -> int:
        """Rows go to the mapped database and table. The target commits just after we confirm we still own the job."""
        label = f"{mapping.connection.name} → {rule.table}"
        job.target = label
        LoadJob.objects.filter(pk=job.pk).update(target=label)
        log.info("[%s] %s goes to %s%s%s", job.profile_name, job.filename, label, " (create if missing)" if rule.create_table else "",
                 f" (merge on {rule.merge_keys})" if rule.merge_keys else "")

        def still_ours():
            kept = LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=self.name).update(
                lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS)
            )
            if not kept:
                raise LeaseLost("the lease expired while loading, so the load into the target was rolled back")

        count = targets.load_file(parser, path, mapping.connection, rule.table, rule.create_table, cfg.batch_size, before_commit=still_ours,
                                  merge_keys=key_list(rule.merge_keys))
        owned = LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=self.name).update(
            status=Status.LOADED, rows_loaded=count, error="", finished_at=timezone.now(), lease_until=None
        )
        if not owned:  # rows are already committed in the target; another worker may load the file again
            raise LeaseLost(f"{count} rows were committed to {label}, but this job was taken over before it could be marked loaded")
        return count

    def _handle_error(self, job: LoadJob, exc: Exception, cfg):
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (OperationalError, InterfaceError)):
            connection.close()  # a broken connection can't be reused
        if is_transient(exc) and job.attempts < cfg.max_attempts:
            delay = backoff_seconds(job.attempts)
            log.warning("[%s] %s hit a temporary error (attempt %s of %s); retrying in %ss: %s",
                        job.profile_name, job.filename, job.attempts, cfg.max_attempts, int(delay), message)
            release(job, delay=delay, error=f"Attempt {job.attempts} of {cfg.max_attempts} failed, will retry: {message}")
            return
        expected = isinstance(exc, (ParseError, DuplicateFileError, FileNotFoundError, targets.TargetError))
        log.error("[%s] Failed %s: %s", job.profile_name, job.filename, message, exc_info=not expected)
        if not fail(job, message, owner=self.name):
            log.warning("[%s] %s: the job was taken over by another worker", job.profile_name, job.filename)

    # -- liveness: lease renewal and a status file the engine reads --
    def start_pulse(self):
        self._pulse = threading.Thread(target=self._pulse_loop, daemon=True, name=f"pulse-{self.name}")
        self._pulse.start()

    def stop_pulse(self):
        self._pulse_stop.set()
        if self._pulse:
            self._pulse.join(timeout=5)
        if self.status_file:
            try:
                self.status_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _pulse_loop(self):
        try:
            while not self._pulse_stop.is_set():
                job = self.current
                try:
                    if job is not None:
                        LoadJob.objects.filter(pk=job.pk, status=Status.PROCESSING, worker=self.name).update(
                            lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS)
                        )
                except Exception:  # a missed renewal is fine; the lease has slack
                    log.warning("Worker %s could not renew its lease", self.name, exc_info=True)
                self._write_status(job)
                self._pulse_stop.wait(RENEW_EVERY)
        finally:
            connection.close()

    def _write_status(self, job: LoadJob | None):
        if not self.status_file:
            return
        payload = json.dumps({
            "name": self.name, "pid": os.getpid(), "time": timezone.now().isoformat(),
            "job": job.pk if job else None, "filename": job.filename if job else None,
            "profile": job.profile_name if job else None,
        })
        temp = self.status_file.with_name(self.status_file.name + ".tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(temp, self.status_file)
                    return
                except PermissionError:
                    time.sleep(0.1 * (attempt + 1))
        except OSError:
            pass
