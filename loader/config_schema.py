"""Editable engine settings: what can be changed, how it is validated, where it is stored.

Defaults come from settings.DATALOADER (environment variables). Values changed on the
Configuration page are stored in EngineSetting and win over the defaults. The engine
re-reads them at the start of every scan, so no restart is needed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from .models import EngineSetting
from .parsers import PARSERS


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # "float" | "int" | "bool"
    group: str
    help: str = ""
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None


FIELDS = (
    Field("POLL_INTERVAL", "Poll interval", "float", "Timing", "How long the engine waits between scans when idle.", "seconds", 0.5, 3600),
    Field("SETTLE_SECONDS", "Minimum file age", "float", "Timing", "A file must be unmodified for this long before it is checked.", "seconds", 0, 3600),
    Field("PROBE_SECONDS", "Stability probe", "float", "Timing", "Wait between two size and modified-time checks. A file that changed in between is skipped for now.", "seconds", 0, 600),
    Field("WORKERS", "Worker processes", "int", "Parallelism", "Files loaded at the same time, each in its own process. 0 = the engine loads files itself, one at a time. SQLite allows one writer, so use PostgreSQL or another server database to gain speed.", "processes", 0, 64),
    Field("MAX_ATTEMPTS", "Attempts per file", "int", "Parallelism", "A file that hits a temporary error (lost connection, deadlock, locked file) is retried, waiting longer each time, until this many attempts are used.", "attempts", 1, 20),
    Field("BATCH_SIZE", "Insert batch size", "int", "Loading", "Rows written to the database per insert.", "rows", 1, 100000),
    Field("REJECT_DUPLICATES", "Reject duplicate files", "bool", "Loading", "Send a file to the fail folder if identical contents were already loaded."),
)
BY_KEY = {f.key: f for f in FIELDS}


FALLBACKS = {"WORKERS": 1, "MAX_ATTEMPTS": 3}  # for a settings.py written before these existed


def defaults() -> dict:
    return {f.key: settings.DATALOADER.get(f.key, FALLBACKS.get(f.key)) for f in FIELDS}


def overrides() -> dict:
    return {k: v for k, v in EngineSetting.objects.values_list("key", "value") if k in BY_KEY}


def effective() -> dict:
    return {**defaults(), **overrides()}


# ---- validation -----------------------------------------------------------
def _number(n: float) -> str:
    return f"{n:g}"


def _coerce(field: Field, raw):
    if field.kind == "bool":
        if not isinstance(raw, bool):
            raise ValueError("Must be on or off.")
        return raw

    if field.kind in ("float", "int"):
        if isinstance(raw, bool) or raw is None or (isinstance(raw, str) and not raw.strip()):
            raise ValueError("Enter a number.")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Enter a number.") from None
        if not math.isfinite(number):
            raise ValueError("Enter a number.")
        if field.kind == "int":
            if number != int(number):
                raise ValueError("Enter a whole number.")
            number = int(number)
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"Must be at least {_number(field.minimum)}.")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"Must be at most {_number(field.maximum)}.")
        return number

    raise ValueError("Unsupported setting type.")


def validate(payload: dict):
    """Returns (clean_values, errors). Saves nothing."""
    clean, errors = {}, {}
    for key, raw in payload.items():
        field = BY_KEY.get(key)
        if field is None:
            errors[key] = "Unknown setting."
            continue
        try:
            clean[key] = _coerce(field, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    return clean, errors


# ---- persistence ----------------------------------------------------------
def save(clean: dict) -> None:
    base = defaults()
    with transaction.atomic():
        for key, value in clean.items():
            if value == base[key]:
                EngineSetting.objects.filter(key=key).delete()  # back to default: no override
            else:
                EngineSetting.objects.update_or_create(key=key, defaults={"value": value})


def reset(keys=None) -> None:
    queryset = EngineSetting.objects.all()
    if keys:
        queryset = queryset.filter(key__in=keys)
    queryset.delete()


def describe() -> dict:
    base, custom = defaults(), overrides()
    fields = [
        {
            "key": f.key,
            "label": f.label,
            "kind": f.kind,
            "group": f.group,
            "help": f.help,
            "unit": f.unit,
            "min": f.minimum,
            "max": f.maximum,
            "value": custom.get(f.key, base[f.key]),
            "default": base[f.key],
            "custom": f.key in custom,
        }
        for f in FIELDS
    ]
    readonly = [
        {"label": "Supported file types", "value": ", ".join(list(PARSERS))},
        {"label": "Heartbeat file", "value": str(settings.DATALOADER["HEARTBEAT_FILE"])},
        {"label": "Database", "value": settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]},
    ]
    return {"fields": fields, "readonly": readonly}
