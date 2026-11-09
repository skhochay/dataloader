"""The file-drop engine: a scanner that feeds a work queue, and a supervisor for the workers that drain it.

    stage/ --scan--> processing/ (job QUEUED) --worker--> archive/   (job LOADED)
                                                      \\--> fail/  (+ <file>.error.txt)

Each enabled directory profile (see the Directories page) is one lane with its own four folders.
The scanner probes each file in each lane's stage folder, moves ready files into the lane's
processing folder and records a QUEUED job for each. Workers (separate processes, see worker.py and
the Workers setting) claim jobs and load them, so several files load at once. With Workers = 0 the
engine loads files itself, one at a time.

Guarantees
  * A file is loaded all-or-nothing: one DB transaction per file.
  * A file is never read while still being written: it must be older than
    SETTLE_SECONDS, unchanged (size and modified time) across a PROBE_SECONDS
    probe, and not held open by another process. Dot-files, .part and .tmp
    files are ignored.
  * A crash loses nothing. A job whose worker dies is queued again when its lease runs out, and
    the load itself rolled back. Files left in processing/ with no job go back to stage/.
  * Every attempt is recorded in LoadJob, including failures.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import config_schema
from .models import DirectoryProfile, LoadJob
from .worker import ERROR_SIDECAR, Worker, release_all_processing, requeue_expired, requeue_worker

log = logging.getLogger("loader.engine")

IGNORED_SUFFIXES = (".part", ".tmp", ".crdownload", ".swp")


def is_candidate_name(name: str) -> bool:
    """True for names the engine treats as real files (not hidden/in-flight/notes)."""
    lowered = name.lower()
    return not (name.startswith(".") or lowered.endswith(IGNORED_SUFFIXES) or lowered.endswith(ERROR_SIDECAR))


def snapshot(path: Path):
    """(size, mtime_ns), or None if the file has vanished."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def is_unlocked(path: Path) -> bool:
    """False if another process still holds the file open for writing (Windows copies do).

    Opens read/write without changing anything. A read-only file is not a locked
    file, so a permission error only counts as a lock if the file is writable.
    """
    try:
        with open(path, "rb+"):
            return True
    except FileNotFoundError:
        return False
    except PermissionError:
        return not os.access(path, os.W_OK)


@dataclass(frozen=True)
class Lane:
    """The four folders of one directory profile."""

    id: int | None
    name: str
    stage: Path
    processing: Path
    archive: Path
    fail: Path

    @property
    def directories(self):
        return (self.stage, self.processing, self.archive, self.fail)

    @classmethod
    def from_profile(cls, p: DirectoryProfile) -> "Lane":
        return cls(p.pk, p.name, Path(p.stage_dir), Path(p.processing_dir), Path(p.archive_dir), Path(p.fail_dir))


def enabled_lanes() -> list[Lane]:
    return [Lane.from_profile(p) for p in DirectoryProfile.objects.filter(enabled=True).order_by("id")]


@dataclass(frozen=True)
class EngineConfig:
    heartbeat: Path
    poll_interval: float = 5.0
    settle_seconds: float = 2.0
    probe_seconds: float = 2.0
    batch_size: int = 1000
    reject_duplicates: bool = True
    workers: int = 0  # worker processes; 0 = the engine loads files itself
    max_attempts: int = 3

    @classmethod
    def from_settings(cls) -> "EngineConfig":
        """Defaults from settings.py with anything edited on the Configuration page applied."""
        c = config_schema.effective()
        return cls(
            heartbeat=Path(settings.DATALOADER["HEARTBEAT_FILE"]),
            poll_interval=float(c["POLL_INTERVAL"]),
            settle_seconds=float(c["SETTLE_SECONDS"]),
            probe_seconds=float(c["PROBE_SECONDS"]),
            batch_size=int(c["BATCH_SIZE"]),
            reject_duplicates=bool(c["REJECT_DUPLICATES"]),
            workers=int(c["WORKERS"]),
            max_attempts=int(c["MAX_ATTEMPTS"]),
        )

    @property
    def stop_file(self) -> Path:
        return self.heartbeat.with_name("engine.stop")

    @property
    def log_file(self) -> Path:
        return self.heartbeat.with_name("engine.log")

    @property
    def workers_dir(self) -> Path:
        """Per-worker status (<slot>.json) and stop-request (<slot>.stop) files."""
        return self.heartbeat.with_name("workers")


class Engine:
    HEARTBEAT_EVERY = 2  # seconds

    def __init__(self, config: EngineConfig | None = None, lanes: list[Lane] | None = None):
        self._pinned = config is not None  # a fixed config (tests) is never reloaded
        self._fixed_lanes = lanes  # fixed lanes (tests) replace the profiles in the database
        self.cfg = config or EngineConfig.from_settings()
        self.lanes: list[Lane] = []
        self._ready: set[Lane] = set()  # lanes already prepared and recovered
        self._broken: set[Lane] = set()  # lanes whose folders could not be created (logged once)
        self._workers: dict[int, subprocess.Popen] = {}  # slot -> child process
        self._stop = threading.Event()
        self._halt = threading.Event()  # ends the heartbeat thread, after the workers are gone
        self._inline = Worker(lambda: self.cfg, "inline", stop=self._stop)
        self._started_at = timezone.now().isoformat()

    # -- lifecycle ---------------------------------------------------------
    def stop(self, *_args):
        if not self._stop.is_set():
            log.info("Stop requested; workers finish their current file first.")
        self._stop.set()

    def prepare(self):
        self.cfg.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.workers_dir.mkdir(parents=True, exist_ok=True)
        release_all_processing()  # no worker of ours is running yet, so anything "loading" is a leftover
        self._sync_lanes()

    def run_forever(self):
        self.prepare()
        self.cfg.stop_file.unlink(missing_ok=True)  # a leftover request must not stop a fresh start
        for leftover in self.cfg.workers_dir.glob("worker-*"):
            leftover.unlink(missing_ok=True)
        self._started_at = timezone.now().isoformat()
        beat = threading.Thread(target=self._beat, daemon=True, name="heartbeat")
        beat.start()
        log.info(
            "Engine started (PID %s, poll every %ss, %s worker process(es)), watching %s profile(s): %s",
            os.getpid(), self.cfg.poll_interval, self.cfg.workers, len(self.lanes), ", ".join(l.name for l in self.lanes) or "none",
        )
        while not self._stop.is_set():
            try:
                handled = self.run_once()
                self._supervise_workers()
            except Exception:  # keep the engine alive through DB blips etc.
                log.exception("Engine loop error; retrying after the poll interval.")
                handled = 0
            if not handled:
                self._stop.wait(self.cfg.poll_interval)
        self._stop_workers()
        self._halt.set()
        beat.join(timeout=5)
        for path in (self.cfg.heartbeat, self.cfg.stop_file):  # so the dashboard shows "stopped" at once
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        log.info("Engine stopped.")

    def run_once(self) -> int:
        """One scan: queue every ready file in every enabled profile.

        With Workers = 0 the engine then loads the queue itself and the result is the number of files
        loaded; otherwise it is the number of files queued (the workers load them).
        """
        close_old_connections()
        self._reload_config()
        lanes = self._sync_lanes()
        requeue_expired(self.cfg.max_attempts)
        queued = 0
        for lane, path in self._ready_files(lanes):
            if self._stop.is_set():
                break
            if self._enqueue(path, lane):
                queued += 1
        if self.cfg.workers == 0:
            return self._inline.drain()
        return queued

    # -- internals ---------------------------------------------------------
    def _reload_config(self):
        """Pick up changes made on the Configuration page. Runs between scans, never mid-load."""
        if self._pinned:
            return
        new = EngineConfig.from_settings()
        if new == self.cfg:
            return
        changed = [f.name for f in fields(new) if getattr(new, f.name) != getattr(self.cfg, f.name)]
        log.info("Settings changed (%s); applying.", ", ".join(changed))
        self.cfg = new

    def _sync_lanes(self) -> list[Lane]:
        """The profiles to serve right now: enabled ones from the database, folders created, strays re-queued.

        A profile whose folders can't be created (a missing drive, say) is skipped and logged once;
        the others keep running.
        """
        wanted = self._fixed_lanes if self._fixed_lanes is not None else enabled_lanes()
        usable = []
        for lane in wanted:
            try:
                for directory in lane.directories:
                    directory.mkdir(parents=True, exist_ok=True)
                if lane not in self._ready:
                    self._recover(lane)
                    self._ready.add(lane)
            except OSError as exc:
                if lane not in self._broken:
                    log.error("Profile %s skipped: cannot use its folders (%s)", lane.name, exc)
                    self._broken.add(lane)
                continue
            if lane in self._broken:
                log.info("Profile %s is usable again", lane.name)
                self._broken.discard(lane)
            usable.append(lane)
        if self.lanes and usable != self.lanes:
            log.info("Profiles changed; now watching: %s", ", ".join(l.name for l in usable) or "none")
        self.lanes = usable
        return usable

    def _recover(self, lane: Lane):
        """Files in processing/ that no queued or running job owns (a crash between move and record) go back to stage."""
        owned = set(
            LoadJob.objects.filter(status__in=[LoadJob.Status.QUEUED, LoadJob.Status.PROCESSING]).values_list("path", flat=True)
        )
        for path in lane.processing.iterdir():
            if path.is_file() and str(path) not in owned:
                target = self._unique(lane.stage / path.name)
                shutil.move(str(path), str(target))
                log.warning("[%s] Recovered %s back to stage", lane.name, path.name)

    # -- workers --
    def _worker_command(self, slot: int) -> list[str]:
        return [sys.executable, str(settings.BASE_DIR / "manage.py"), "run_worker", "--slot", str(slot), "--parent", str(os.getpid())]

    def _spawn_worker(self, slot: int):
        (self.cfg.workers_dir / f"worker-{slot}.stop").unlink(missing_ok=True)
        extra = {}
        if os.name == "nt":
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with open(self.cfg.log_file, "ab") as out:
            process = subprocess.Popen(
                self._worker_command(slot), cwd=settings.BASE_DIR, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, close_fds=True, **extra,
            )
        self._workers[slot] = process
        log.info("Started worker %s (PID %s)", slot, process.pid)

    def _supervise_workers(self):
        """Keep exactly Workers child processes running: restart dead ones, start or stop to match the setting."""
        if self._stop.is_set():
            return
        wanted = max(self.cfg.workers, 0)
        for slot, process in list(self._workers.items()):
            if process.poll() is not None:
                del self._workers[slot]
                (self.cfg.workers_dir / f"worker-{slot}.json").unlink(missing_ok=True)
                requeue_worker(f"worker-{slot}-{process.pid}", self.cfg.max_attempts)  # its file is not lost
                if slot <= wanted:
                    log.error("Worker %s stopped unexpectedly (exit code %s); starting a new one.", slot, process.returncode)
        for slot in self._workers:
            if slot > wanted:  # too many: ask the extras to finish their file and leave
                (self.cfg.workers_dir / f"worker-{slot}.stop").touch()
        for slot in range(1, wanted + 1):
            if slot not in self._workers:
                self._spawn_worker(slot)

    def _stop_workers(self):
        """Ask every worker to finish its current file and exit, and wait for them."""
        for slot in self._workers:
            try:
                (self.cfg.workers_dir / f"worker-{slot}.stop").touch()
            except OSError:
                pass
        while self._workers:
            for slot, process in list(self._workers.items()):
                if process.poll() is not None:
                    del self._workers[slot]
            if self._workers:
                time.sleep(0.5)
        log.info("All workers stopped.")

    def _worker_report(self) -> list[dict]:
        report = []
        for slot, process in sorted(self._workers.items()):
            entry = {"slot": slot, "pid": process.pid, "job": None, "filename": None, "profile": None}
            try:
                entry.update(json.loads((self.cfg.workers_dir / f"worker-{slot}.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            entry["slot"], entry["pid"] = slot, process.pid
            report.append(entry)
        return report

    # -- heartbeat --
    def _write_heartbeat(self):
        payload = json.dumps(
            {
                "pid": os.getpid(), "started_at": self._started_at, "time": timezone.now().isoformat(),
                "workers": self._worker_report(), "worker_target": self.cfg.workers,
            }
        )
        temp = self.cfg.heartbeat.with_name(self.cfg.heartbeat.name + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        # Readers never see a half-written file. On Windows the replace fails briefly if the
        # dashboard has the file open at that instant, so retry a few times.
        for attempt in range(5):
            try:
                os.replace(temp, self.cfg.heartbeat)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def _check_stop_file(self):
        """The dashboard asks for a stop by creating this file."""
        if self.cfg.stop_file.exists():
            self.stop()

    def _beat(self):
        while not self._halt.is_set():
            try:
                self._write_heartbeat()
            except OSError:
                log.warning("Could not update heartbeat file %s", self.cfg.heartbeat)
            try:
                self._check_stop_file()
            except OSError:
                pass
            self._halt.wait(self.HEARTBEAT_EVERY)

    def _pause(self, seconds: float):
        self._stop.wait(seconds)

    # -- scanning --
    def _ready_files(self, lanes: list[Lane]):
        """(lane, path) pairs that are safe to load: old enough, unchanged across a probe, and unlocked.

        Probe: record each candidate's size and modified time, wait PROBE_SECONDS once for all
        profiles, record them again. A file that grew, was touched, or is still held open by
        the writer is skipped this scan and looked at again on the next one.
        """
        cutoff = time.time() - self.cfg.settle_seconds
        before = {}
        for lane in lanes:
            try:
                entries = list(lane.stage.iterdir())
            except OSError:
                continue
            for path in entries:
                if not is_candidate_name(path.name) or not path.is_file():
                    continue
                snap = snapshot(path)
                if snap is not None and snap[1] / 1e9 <= cutoff:
                    before[path] = (lane, snap)

        if before and self.cfg.probe_seconds > 0:
            self._pause(self.cfg.probe_seconds)

        ready = []
        for path, (lane, first) in before.items():
            second = snapshot(path)
            if second is None:
                continue
            if second != first:
                log.info("[%s] %s is still changing; will check again", lane.name, path.name)
            elif not is_unlocked(path):
                log.info("[%s] %s is still open in another process; will check again", lane.name, path.name)
            else:
                ready.append((second[1], lane, path))
        ready.sort(key=lambda item: item[0])
        return [(lane, path) for _, lane, path in ready]

    def _enqueue(self, path: Path, lane: Lane) -> bool:
        """Claim a file by renaming it into processing/, then record a QUEUED job for it."""
        claimed = lane.processing / f"{uuid.uuid4().hex[:8]}_{path.name}"  # unique, so same-named files never collide
        try:
            os.replace(path, claimed)  # atomic: same filesystem
        except FileNotFoundError:
            return False  # another actor removed it first
        except PermissionError:
            log.info("[%s] %s is locked; will retry on the next scan", lane.name, path.name)
            return False
        try:
            LoadJob.objects.create(
                status=LoadJob.Status.QUEUED, filename=path.name, path=str(claimed),
                profile_id=lane.id, profile_name=lane.name,
                archive_dir=str(lane.archive), fail_dir=str(lane.fail),
                size_bytes=claimed.stat().st_size,
            )
        except Exception:
            try:
                os.replace(claimed, path)  # put it back so nothing is lost while the database is away
            except OSError:
                pass
            raise
        log.info("[%s] Queued %s", lane.name, path.name)
        return True

    @staticmethod
    def _unique(path: Path) -> Path:
        candidate, n = path, 1
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
            n += 1
        return candidate
