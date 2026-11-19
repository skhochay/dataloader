"""Start, stop and inspect the engine process from the web app.

The engine is a separate OS process. It reports itself through a heartbeat file (JSON with
its PID), refreshed every couple of seconds. Stopping is a polite request: the web app creates
a stop file, the engine sees it, finishes the file it is loading, and exits. A force stop ends
the process immediately; the interrupted load rolls back and the file is re-queued on the
next start.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from django.conf import settings

ONLINE_WITHIN = 10  # seconds: a heartbeat newer than this means the engine is running

_children: list[subprocess.Popen] = []  # engines started by this web process, kept so they can be reaped
_lock = threading.Lock()


class EngineError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _psutil():
    try:
        import psutil

        return psutil
    except ImportError:
        return None


def _pid_alive(pid: int) -> bool:
    psutil = _psutil()
    if psutil is None:
        return True  # can't check; trust the heartbeat's freshness
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.AccessDenied:
        return True
    except psutil.Error:
        return False


def _reap():
    for child in _children[:]:
        if child.poll() is not None:
            _children.remove(child)


def tail(path, lines: int = 8) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 4000, 0))
            return "\n".join(fh.read().decode("utf-8", "replace").splitlines()[-lines:])
    except OSError:
        return ""


def read_state(cfg) -> dict:
    _reap()
    info, age = None, None
    try:
        age = time.time() - cfg.heartbeat.stat().st_mtime
        info = json.loads(cfg.heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    pid = info.get("pid") if isinstance(info, dict) else None
    running = bool(age is not None and age < ONLINE_WITHIN and isinstance(pid, int) and _pid_alive(pid))
    return {
        "running": running,
        "pid": pid if running else None,
        "started_at": info.get("started_at") if running else None,
        "stopping": running and cfg.stop_file.exists(),
        "can_force": _psutil() is not None,
        "workers": info.get("workers", []) if running else [],
        "worker_target": info.get("worker_target") if running else None,
        "log_file": str(cfg.log_file),
    }


def start(cfg) -> dict:
    with _lock:
        if read_state(cfg)["running"]:
            raise EngineError("The engine is already running.")
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.stop_file.unlink(missing_ok=True)

        command = [sys.executable, str(settings.BASE_DIR / "manage.py"), "run_engine"]
        extra = {}
        if os.name == "nt":
            extra["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            )
        else:
            extra["start_new_session"] = True  # survives the web server restarting

        with open(cfg.log_file, "ab") as log:
            process = subprocess.Popen(
                command,
                cwd=settings.BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **extra,
            )
        _children.append(process)

        deadline = time.time() + 3  # fail fast if it dies on startup (bad config, missing migration...)
        while time.time() < deadline:
            if process.poll() is not None:
                raise EngineError(
                    "The engine stopped right after starting.\n" + tail(cfg.log_file), status=500
                )
            if read_state(cfg)["pid"] == process.pid:
                break
            time.sleep(0.2)
        return read_state(cfg)


def request_stop(cfg) -> dict:
    if not read_state(cfg)["running"]:
        raise EngineError("The engine isn’t running.")
    cfg.stop_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return read_state(cfg)


def force_stop(cfg) -> dict:
    psutil = _psutil()
    if psutil is None:
        raise EngineError("Force stop needs the 'psutil' package. Install it with: pip install psutil", status=501)
    state = read_state(cfg)
    if not state["running"]:
        raise EngineError("The engine isn’t running.")
    try:
        process = psutil.Process(state["pid"])
        if "run_engine" not in " ".join(process.cmdline()):
            raise EngineError("That process doesn’t look like the engine, so it was left alone.")
        family = process.children(recursive=True)  # the worker processes
        for victim in [process, *family]:
            try:
                victim.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs([process, *family], timeout=3)
        for victim in alive:
            try:
                victim.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:
        raise EngineError("Not allowed to stop that process.", status=403) from None
    for path in (cfg.heartbeat, cfg.stop_file):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return read_state(cfg)
