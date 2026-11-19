"""Directory profiles: a name plus four folders. Validation, folder checks and listing helpers."""
from __future__ import annotations

import os
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from .models import DirectoryProfile

ROLES = (
    ("stage_dir", "Stage folder", "Drop files here. The engine picks them up."),
    ("processing_dir", "Processing folder", "Holds the one file being loaded right now."),
    ("archive_dir", "Archive folder", "Files that loaded successfully are moved here."),
    ("fail_dir", "Fail folder", "Files that failed are moved here with a note explaining why."),
)
ROLE_KEYS = [k for k, _, _ in ROLES]
SHORT = {"stage_dir": "stage", "processing_dir": "processing", "archive_dir": "archive", "fail_dir": "fail"}
LABEL = {k: label for k, label, _ in ROLES}
# Two profiles may share an archive or fail folder, but never a stage or processing folder.
PRIVATE_KEYS = ("stage_dir", "processing_dir")
FIELDS = ("name", "enabled", "max_parallel", *ROLE_KEYS)
MAX_PARALLEL_LIMIT = 64


def _norm(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _nearest_existing(path: Path):
    while not path.exists():
        if path.parent == path:
            return None
        path = path.parent
    return path


def check_folder(value) -> tuple[bool, str, int | None]:
    """(ok, message, device) for one folder path."""
    if not isinstance(value, str) or not value.strip():
        return False, "Enter a folder path.", None
    path = Path(value.strip())
    if not path.is_absolute():
        return False, "Use a full path, for example C:\\DataLoader\\stage.", None
    anchor = _nearest_existing(path)
    if anchor is None:
        return False, "This drive or location does not exist.", None
    if anchor.exists() and path.exists() and not path.is_dir():
        return False, "This path is a file, not a folder.", None
    if not anchor.is_dir():
        return False, "A part of this path is a file, not a folder.", None
    if not os.access(anchor, os.W_OK | os.X_OK):
        return False, "The engine can’t write to this location.", None
    device = os.stat(anchor).st_dev
    return True, ("Folder exists and is writable." if anchor == path else "Folder will be created."), device


def test_folders(values: dict) -> dict:
    """Check the four folders without saving anything. Returns {ok, results:{key:{ok,message}}, message}."""
    results, devices = {}, {}
    for key in ROLE_KEYS:
        ok, message, device = check_folder(values.get(key))
        results[key] = {"ok": ok, "message": message}
        if device is not None:
            devices[key] = device
    seen: dict[str, list[str]] = {}
    for key in ROLE_KEYS:
        raw = values.get(key)
        if isinstance(raw, str) and raw.strip() and Path(raw.strip()).is_absolute():
            seen.setdefault(_norm(raw.strip()), []).append(key)
    for keys in seen.values():
        if len(keys) > 1:
            for key in keys:
                others = ", ".join(LABEL[k] for k in keys if k != key)
                results[key] = {"ok": False, "message": f"Same folder as {others}. Each folder must be different."}
    if all(r["ok"] for r in results.values()) and len(set(devices.values())) > 1:
        for key in ROLE_KEYS:
            results[key] = {"ok": False, "message": "All four folders must be on the same drive so files move atomically."}
    ok = all(r["ok"] for r in results.values())
    return {"ok": ok, "results": results, "message": "All four folders are usable." if ok else "Some folders need attention."}


def validate(payload: dict, instance: DirectoryProfile | None = None):
    """Returns (clean, errors). Saves nothing. For updates, missing keys keep the saved value."""
    errors = {}
    current = {}
    if instance is not None:
        current = {k: getattr(instance, k) for k in FIELDS}
    merged = {**current, **{k: v for k, v in payload.items() if k in FIELDS}}
    for key in payload:
        if key not in FIELDS:
            errors[key] = "Unknown field."

    name = merged.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = "Enter a profile name."
    else:
        name = name.strip()
        merged["name"] = name
        if len(name) > 64:
            errors["name"] = "Use 64 characters or fewer."
        elif DirectoryProfile.objects.filter(name__iexact=name).exclude(pk=getattr(instance, "pk", None)).exists():
            errors["name"] = "A profile with this name already exists."

    max_parallel = merged.get("max_parallel", 0)
    if isinstance(max_parallel, str) and max_parallel.strip().isdigit():
        max_parallel = int(max_parallel)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or not 0 <= max_parallel <= MAX_PARALLEL_LIMIT:
        errors["max_parallel"] = f"Enter a whole number from 0 to {MAX_PARALLEL_LIMIT} (0 means no limit)."
    merged["max_parallel"] = max_parallel

    enabled = merged.get("enabled", True)
    if not isinstance(enabled, bool):
        errors["enabled"] = "Must be on or off."
    merged["enabled"] = enabled

    report = test_folders(merged)
    for key, result in report["results"].items():
        if not result["ok"]:
            errors[key] = result["message"]

    if not any(k in errors for k in ROLE_KEYS):
        others = DirectoryProfile.objects.exclude(pk=getattr(instance, "pk", None))
        for other in others:
            taken = {_norm(getattr(other, k)): (other.name, k) for k in ROLE_KEYS}
            for key in ROLE_KEYS:
                hit = taken.get(_norm(merged[key]))
                if hit is None:
                    continue
                other_name, other_key = hit
                if key in PRIVATE_KEYS or other_key in PRIVATE_KEYS:
                    errors[key] = f"Already used by profile “{other_name}” ({LABEL[other_key]})."
    if errors:
        return {}, errors
    return {k: merged[k] for k in FIELDS}, {}


# ---- listing --------------------------------------------------------------
def count_files(directory: Path) -> int:
    from .engine import is_candidate_name

    try:
        return sum(1 for p in directory.iterdir() if p.is_file() and is_candidate_name(p.name))
    except OSError:
        return 0


def counts(profile: DirectoryProfile) -> dict:
    return {SHORT[k]: count_files(Path(getattr(profile, k))) for k in ROLE_KEYS}


def list_files(directory: Path, limit: int = 500):
    from .engine import is_candidate_name

    items = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        entries = []
    for path in entries:
        try:
            if path.is_file() and is_candidate_name(path.name):
                st = path.stat()
                items.append(
                    {
                        "name": path.name,
                        "size_bytes": st.st_size,
                        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=dt_timezone.utc).isoformat(),
                    }
                )
        except FileNotFoundError:
            continue  # moved by the engine while we were listing
    items.sort(key=lambda i: i["modified_at"], reverse=True)
    return len(items), items[:limit]


def to_dict(profile: DirectoryProfile, with_counts: bool = True) -> dict:
    data = {
        "id": profile.pk,
        "name": profile.name,
        "enabled": profile.enabled,
        "max_parallel": profile.max_parallel,
        **{k: getattr(profile, k) for k in ROLE_KEYS},
        "jobs": profile.jobs.count(),
    }
    if with_counts:
        data["counts"] = counts(profile)
    return data
