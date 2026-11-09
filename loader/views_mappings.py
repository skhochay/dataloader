"""JSON API for mappings: /api/mappings/..."""
import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods, require_POST

from . import db_connect, mapping, vault
from .models import DatabaseConnection, Mapping


def _body(request):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "The request body must be JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    return payload, None


def _invalid(errors):
    return JsonResponse({"error": "Some fields need attention.", "errors": errors}, status=400)


def _queryset():
    return Mapping.objects.select_related("profile", "connection").prefetch_related("rules")


@require_http_methods(["GET", "POST"])
def collection(request):
    if request.method == "GET":
        return JsonResponse({"mappings": [mapping.to_dict(m) for m in _queryset()]})
    payload, bad = _body(request)
    if bad:
        return bad
    clean, errors = mapping.validate(payload)
    if errors:
        return _invalid(errors)
    return JsonResponse(mapping.to_dict(_queryset().get(pk=mapping.save(clean).pk)), status=201)


@require_http_methods(["GET", "PUT", "DELETE"])
def item(request, pk):
    instance = get_object_or_404(_queryset(), pk=pk)
    if request.method == "DELETE":
        instance.delete()  # the profile, its files and the connection are left alone
        return JsonResponse({"deleted": True})
    if request.method == "PUT":
        payload, bad = _body(request)
        if bad:
            return bad
        clean, errors = mapping.validate(payload, instance)
        if errors:
            return _invalid(errors)
        mapping.save(clean, instance)
        instance = _queryset().get(pk=pk)
    return JsonResponse(mapping.to_dict(instance))


@require_POST
def match(request):
    """Which rule would each file name use? Takes the rules from the form, saved or not."""
    payload, bad = _body(request)
    if bad:
        return bad
    rules, names = payload.get("rules"), payload.get("filenames")
    if not isinstance(rules, list) or not isinstance(names, list):
        return JsonResponse({"error": "Send rules and filenames as lists."}, status=400)
    if len(names) > mapping.MAX_NAMES:
        return JsonResponse({"error": f"Check {mapping.MAX_NAMES} file names or fewer at a time."}, status=400)
    clean = []
    for raw in rules:
        pattern = raw.get("pattern") if isinstance(raw, dict) else None
        if isinstance(pattern, str) and pattern.strip():
            clean.append({"pattern": pattern.strip(), "table": raw.get("table") if isinstance(raw.get("table"), str) else ""})
    names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    return JsonResponse({"results": mapping.try_names(clean, names)})


def _tables_of(payload):
    """The tables the chosen saved connection can see: (tables, truncated, error response)."""
    raw = payload.get("connection")
    connection = DatabaseConnection.objects.filter(pk=raw).first() if isinstance(raw, int) and not isinstance(raw, bool) else None
    if connection is None:
        return None, False, _invalid({"connection": "Choose a database connection."})
    password = ""
    if connection.password_enc:
        try:
            password = vault.decrypt(connection.password_enc)
        except vault.VaultError as exc:
            return None, False, JsonResponse({"ok": False, "message": str(exc)})
    cfg = {
        "kind": connection.kind, "host": connection.host, "port": connection.port, "database": connection.database,
        "username": connection.username, "options": connection.options,
    }
    result = db_connect.list_tables(cfg, password)
    if not result["ok"]:
        return None, False, JsonResponse({"ok": False, "message": result["message"]})
    return result["tables"], result["truncated"], None


@require_POST
def tables(request):
    """Browse: the tables of a saved connection, for picking one in the mapping form."""
    payload, bad = _body(request)
    if bad:
        return bad
    found, truncated, error = _tables_of(payload)
    if error:
        return error
    return JsonResponse({"ok": True, "tables": found, "truncated": truncated})


@require_POST
def verify(request):
    """Does each table exist in the chosen database?"""
    payload, bad = _body(request)
    if bad:
        return bad
    names = payload.get("tables")
    if not isinstance(names, list) or len(names) > mapping.MAX_RULES:
        return JsonResponse({"error": "Send the table names as a list."}, status=400)
    found, truncated, error = _tables_of(payload)
    if error:
        return error
    return JsonResponse({"ok": True, "truncated": truncated, "results": mapping.verify_tables(found, names)})
