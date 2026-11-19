import os
import signal
import threading

from django.core.management.base import BaseCommand

from loader.engine import EngineConfig
from loader.worker import Worker


def parent_alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:
        return True  # can't tell; rely on the stop file
    return psutil.pid_exists(pid)


class Command(BaseCommand):
    help = "A load worker. Started by the engine (see the Workers setting); not meant to be run by hand."

    def add_arguments(self, parser):
        parser.add_argument("--slot", type=int, required=True, help="Worker number (1, 2, ...).")
        parser.add_argument("--parent", type=int, default=0, help="PID of the engine; the worker exits if it disappears.")

    def handle(self, *args, **options):
        slot, parent = options["slot"], options["parent"]
        cfg = EngineConfig.from_settings()
        folder = cfg.workers_dir
        folder.mkdir(parents=True, exist_ok=True)
        stop_file = folder / f"worker-{slot}.stop"
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

        worker = Worker(EngineConfig.from_settings, f"worker-{slot}-{os.getpid()}", folder / f"worker-{slot}.json", stop)
        worker.start_pulse()
        try:
            while not stop.is_set():
                if stop_file.exists() or (parent and not parent_alive(parent)):
                    break
                handled = worker.drain()
                if not handled:
                    stop.wait(min(EngineConfig.from_settings().poll_interval, 1.0))
        finally:
            worker.stop_pulse()
            stop_file.unlink(missing_ok=True)
