import os
import signal

from django.core.management.base import BaseCommand, CommandError

from loader import engine_control
from loader.engine import Engine


class Command(BaseCommand):
    help = "Watch the stage directory and load files into the database."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process what is in stage now, then exit.")

    def handle(self, *args, **options):
        engine = Engine()
        if options["once"]:
            engine.prepare()
            count = engine.run_once()
            self.stdout.write(f"Processed {count} file(s).")
            return
        state = engine_control.read_state(engine.cfg)
        if state["running"] and state["pid"] != os.getpid():
            raise CommandError(f"The engine is already running (PID {state['pid']}).")
        signal.signal(signal.SIGINT, engine.stop)
        signal.signal(signal.SIGTERM, engine.stop)
        engine.run_forever()
