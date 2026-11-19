import json
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods, require_POST

from . import db_connect, vault
from .db_kinds import KINDS, public_kinds
from .models import DatabaseConnection


def _json_body(request):
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return None, JsonResponse({"error": "The request body must be JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    return payload, None


def _invalid(errors):
    return JsonResponse({"error": "Some fields need attention.", "errors": errors}, status=400)


def _as_dict(conn: DatabaseConnection) -> dict:
    return {
        "id": conn.pk,
        "name": conn.name,
        "kind": conn.kind,
        "host": conn.host,
        "port": conn.port,
        "database": conn.database,
        "username": conn.username,
        "has_password": bool(conn.password_enc),  # the password itself is never sent to the browser
        "options": conn.options,
    }


# ---- validation -------------------------------------------------------------------
def _coerce_option(option, raw):
    kind = option["type"]
    if kind == "bool":
        if raw in (None, ""):
            return bool(option.get("default", False))
        if not isinstance(raw, bool):
            raise ValueError("Must be on or off.")
        return raw
    if kind == "select":
        value = raw if raw not in (None, "") else option.get("default")
        if value not in [c["value"] for c in option["choices"]]:
            raise ValueError("Choose one of the listed values.")
        return value
    text = ("" if raw is None else str(raw)).strip()
    if len(text) > 2000:
        raise ValueError("That is too long.")
    return text


def validate_payload(payload, instance=None, require_name=True):
    """Returns (clean, errors). clean holds only fields the chosen database type uses."""
    spec = KINDS.get(payload.get("kind"))
    if spec is None:
        return {}, {"kind": "Choose a database type."}
    errors = {}
    clean = {"kind": payload["kind"]}

    name = str(payload.get("name") or "").strip()
    if require_name:
        if not name:
            errors["name"] = "Enter a name."
        elif len(name) > 64:
            errors["name"] = "Use 64 characters or fewer."
        else:
            taken = DatabaseConnection.objects.filter(name__iexact=name)
            if instance is not None:
                taken = taken.exclude(pk=instance.pk)
            if taken.exists():
                errors["name"] = "Another connection already uses this name."
    clean["name"] = name

    for field in ("host", "database", "username"):
        definition = spec["fields"].get(field)
        value = str(payload.get(field) or "").strip() if definition else ""
        if definition and definition["required"] and not value:
            errors[field] = f"{definition['label']} is required."
        elif len(value) > (1024 if field == "database" else 255):
            errors[field] = "That is too long."
        elif field == "database" and payload["kind"] == "sqlite" and value and not Path(value).is_absolute():
            errors[field] = "Use a full path, for example C:\\Data\\app.db"
        clean[field] = value

    port = None
    if "port" in spec["fields"]:
        raw = payload.get("port")
        if raw not in (None, ""):
            try:
                port = int(str(raw).strip())
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                errors["port"] = "Enter a port number between 1 and 65535."
                port = None
    clean["port"] = port

    raw_options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    options = {}
    for option in spec["options"]:
        try:
            options[option["key"]] = _coerce_option(option, raw_options.get(option["key"]))
        except ValueError as exc:
            errors[option["key"]] = str(exc)
    clean["options"] = options
    if payload["kind"] == "odbc" and not (options.get("dsn") or options.get("driver")):
        errors["dsn"] = "Enter a DSN or a driver name."
        errors["driver"] = "Enter a DSN or a driver name."
    return clean, errors


def _apply_password(instance, payload):
    """Typed password replaces the saved one; blank keeps it; clear_password removes it."""
    if payload.get("clear_password") is True:
        instance.password_enc = ""
    typed = payload.get("password")
    if isinstance(typed, str) and typed:
        try:
            instance.password_enc = vault.encrypt(typed)
        except vault.VaultError as exc:
            return str(exc)
    return None


# ---- endpoints --------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def collection(request):
    if request.method == "POST":
        payload, bad = _json_body(request)
        if bad:
            return bad
        clean, errors = validate_payload(payload)
        if errors:
            return _invalid(errors)
        instance = DatabaseConnection(**clean)
        problem = _apply_password(instance, payload)
        if problem:
            return _invalid({"password": problem})
        instance.save()
        return JsonResponse(_as_dict(instance), status=201)
    return JsonResponse(
        {"connections": [_as_dict(c) for c in DatabaseConnection.objects.all()], "kinds": public_kinds()}
    )


@require_http_methods(["GET", "PUT", "DELETE"])
def item(request, pk):
    instance = get_object_or_404(DatabaseConnection, pk=pk)
    if request.method == "DELETE":
        used_by = [m.profile.name for m in instance.mappings.select_related("profile")]
        if used_by:
            return JsonResponse(
                {"error": f"Used by the mapping of profile {', '.join(f'“{n}”' for n in used_by)}. Delete that mapping first."},
                status=409,
            )
        instance.delete()
        return JsonResponse({"deleted": True})
    if request.method == "PUT":
        payload, bad = _json_body(request)
        if bad:
            return bad
        clean, errors = validate_payload(payload, instance)
        if errors:
            return _invalid(errors)
        for field, value in clean.items():
            setattr(instance, field, value)
        problem = _apply_password(instance, payload)
        if problem:
            return _invalid({"password": problem})
        instance.save()
    return JsonResponse(_as_dict(instance))


@require_POST
def test(request):
    """Test the values in the form (saved or not). Blank password on a saved connection uses the stored one."""
    payload, bad = _json_body(request)
    if bad:
        return bad
    instance = None
    if payload.get("id"):
        instance = DatabaseConnection.objects.filter(pk=payload["id"]).first()
    clean, errors = validate_payload(payload, instance, require_name=False)
    if errors:
        return _invalid(errors)

    password = payload.get("password") if isinstance(payload.get("password"), str) else ""
    if not password and instance and instance.password_enc and payload.get("clear_password") is not True:
        try:
            password = vault.decrypt(instance.password_enc)
        except vault.VaultError as exc:
            return JsonResponse({"ok": False, "message": str(exc)})
    return JsonResponse(db_connect.test_connection(clean, password))
