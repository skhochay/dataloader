"""JSON API for directory profiles: /api/directories/..."""
import json
import os
import uuid
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import profiles
from .models import DirectoryProfile
from .parsers import PARSERS


def _body(request):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "The request body must be JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    return payload, None


def _invalid(errors):
    return JsonResponse({"error": "Some fields are invalid.", "errors": errors}, status=400)


@require_http_methods(["GET", "POST"])
def collection(request):
    if request.method == "GET":
        return JsonResponse({"profiles": [profiles.to_dict(p) for p in DirectoryProfile.objects.all()]})
    payload, error = _body(request)
    if error:
        return error
    clean, errors = profiles.validate(payload)
    if errors:
        return _invalid(errors)
    profile = DirectoryProfile.objects.create(**clean)
    return JsonResponse(profiles.to_dict(profile), status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def item(request, pk):
    profile = get_object_or_404(DirectoryProfile, pk=pk)
    if request.method == "DELETE":
        # Jobs keep their profile name. The folders and any files in them are left alone.
        profile.delete()
        return JsonResponse({"deleted": True})
    if request.method == "PUT":
        payload, error = _body(request)
        if error:
            return error
        clean, errors = profiles.validate(payload, instance=profile)
        if errors:
            return _invalid(errors)
        for key, value in clean.items():
            setattr(profile, key, value)
        profile.save()
        if "name" in clean:
            profile.jobs.update(profile_name=profile.name)
    return JsonResponse(profiles.to_dict(profile))


@require_POST
def test(request):
    """Check four folders without saving. The body holds the form's current values."""
    payload, error = _body(request)
    if error:
        return error
    return JsonResponse(profiles.test_folders(payload))


@require_GET
def files(request, pk, name):
    profile = get_object_or_404(DirectoryProfile, pk=pk)
    key = {v: k for k, v in profiles.SHORT.items()}.get(name)
    if key is None:
        return JsonResponse({"error": "Unknown directory."}, status=404)
    directory = Path(getattr(profile, key))
    total, items = profiles.list_files(directory)
    return JsonResponse({"directory": str(directory), "total": total, "files": items})


@require_POST
def upload(request, pk):
    profile = get_object_or_404(DirectoryProfile, pk=pk)
    uploaded = request.FILES.get("file")
    if uploaded is None:
        return JsonResponse({"error": "No file was attached."}, status=400)
    name = get_valid_filename(Path(uploaded.name).name)
    suffix = Path(name).suffix.lower()
    if not name or suffix not in PARSERS:
        return JsonResponse(
            {"error": f"Unsupported file type. Use one of: {', '.join(list(PARSERS))}."}, status=400
        )
    stage = Path(profile.stage_dir)
    try:
        stage.mkdir(parents=True, exist_ok=True)
        # Write under a hidden .part name, then rename: the engine never sees a partial file.
        temp = stage / f".upload-{uuid.uuid4().hex}.part"
        with open(temp, "wb") as out:
            for chunk in uploaded.chunks():
                out.write(chunk)
        final = stage / name
        if final.exists():
            final = stage / f"{final.stem}-{uuid.uuid4().hex[:6]}{final.suffix}"
        os.replace(temp, final)
    except OSError as exc:
        return JsonResponse({"error": f"Could not write to the stage folder: {exc}"}, status=500)
    return JsonResponse({"filename": final.name, "profile": profile.name}, status=201)
