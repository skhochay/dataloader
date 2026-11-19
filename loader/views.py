import json
import shutil
import uuid
from pathlib import Path

from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import config_schema, engine_control, profiles
from .engine import ERROR_SIDECAR, EngineConfig
from .worker import queue_counts
from .models import DirectoryProfile, LoadJob
from .parsers import PARSERS


def _int(value, default):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def _can_retry(job: LoadJob) -> bool:
    return job.status == LoadJob.Status.FAILED and bool(job.final_path) and Path(job.final_path).is_file()


def _job_dict(job: LoadJob) -> dict:
    return {
        "id": job.pk,
        "profile": job.profile_name,
        "filename": job.filename,
        "status": job.status,
        "rows_loaded": job.rows_loaded,
        "size_bytes": job.size_bytes,
        "checksum": job.checksum,
        "error": job.error,
        "final_path": job.final_path,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "can_retry": _can_retry(job),
        "attempts": job.attempts,
        "worker": job.worker,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "target": job.target,
    }


@ensure_csrf_cookie
@require_GET
def stats(request):
    cfg = EngineConfig.from_settings()
    engine = engine_control.read_state(cfg)
    jobs_by_status = {r["status"]: r["n"] for r in LoadJob.objects.values("status").annotate(n=Count("id"))}
    profile_list = [profiles.to_dict(p) for p in DirectoryProfile.objects.all()]
    totals = {"stage": 0, "processing": 0, "archive": 0, "fail": 0}
    for p in profile_list:
        if p["enabled"]:
            for key in totals:
                totals[key] += p["counts"][key]
    return JsonResponse(
        {
            "engine_online": engine["running"],
            "engine": engine,
            "directories": totals,  # across enabled profiles
            "profiles": profile_list,
            "jobs": jobs_by_status,
            "queue": queue_counts(),
            "supported_types": list(PARSERS),
        }
    )


def _engine_response(action):
    try:
        return JsonResponse(action())
    except engine_control.EngineError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)


@require_POST
def engine_start(request):
    return _engine_response(lambda: engine_control.start(EngineConfig.from_settings()))


@require_POST
def engine_stop(request):
    try:
        force = bool(json.loads(request.body or b"{}").get("force"))
    except (ValueError, AttributeError):
        force = False
    cfg = EngineConfig.from_settings()
    return _engine_response(lambda: engine_control.force_stop(cfg) if force else engine_control.request_stop(cfg))


@require_GET
def jobs(request):
    queryset = LoadJob.objects.all()
    wanted = [s for s in request.GET.get("status", "").split(",") if s in LoadJob.Status.values]
    if wanted:
        queryset = queryset.filter(status__in=wanted)
    limit = min(_int(request.GET.get("limit"), 50), 200)
    offset = _int(request.GET.get("offset"), 0)
    return JsonResponse(
        {"total": queryset.count(), "jobs": [_job_dict(j) for j in queryset[offset : offset + limit]]}
    )


@require_GET
def job_detail(request, pk):
    job = get_object_or_404(LoadJob, pk=pk)
    sample = list(job.records.order_by("row_number").values("row_number", "data")[:20])
    return JsonResponse({**_job_dict(job), "sample_rows": sample})


@require_POST
def retry(request, pk):
    job = get_object_or_404(LoadJob, pk=pk)
    if not _can_retry(job):
        return JsonResponse({"error": "This file is no longer in the fail directory."}, status=409)
    if job.profile is None:
        return JsonResponse({"error": "The profile this file belonged to was deleted."}, status=409)
    stage = Path(job.profile.stage_dir)
    source = Path(job.final_path)
    destination = stage / job.filename
    if destination.exists():
        destination = stage / f"{destination.stem}-retry{uuid.uuid4().hex[:6]}{destination.suffix}"
    shutil.move(str(source), str(destination))
    Path(str(source) + ERROR_SIDECAR).unlink(missing_ok=True)
    job.status = LoadJob.Status.REQUEUED
    job.final_path = ""
    job.save(update_fields=["status", "final_path"])
    return JsonResponse(_job_dict(job))


@require_http_methods(["GET", "PUT"])
def config(request):
    if request.method == "PUT":
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "The request body must be JSON."}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
        clean, errors = config_schema.validate(payload)
        if errors:
            return JsonResponse({"error": "Some settings are invalid.", "errors": errors}, status=400)
        config_schema.save(clean)
    return JsonResponse(config_schema.describe())


@require_POST
def config_reset(request):
    config_schema.reset()
    return JsonResponse(config_schema.describe())
