"""Mappings: which database a profile loads into, and which file names go to which table.

A mapping belongs to one directory profile and names one saved database connection. Its rules are
checked from the top, and the first pattern that matches a file name decides the table:

    emp*.*  =>  dbo.EMP
    dep*.*  =>  delta.DEP

Patterns use * (any characters, also none) and ? (one character), and ignore upper/lower case.
They are matched against the file name only, never the folder.
"""
from __future__ import annotations

import fnmatch
import re

from django.db import transaction

from .models import DatabaseConnection, DirectoryProfile, Mapping, MappingRule

MAX_RULES = 200
MAX_NAMES = 500
_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]*$")
FIELDS = ("profile", "connection", "enabled", "rules")


def matches(pattern: str, filename: str) -> bool:
    return fnmatch.fnmatchcase(filename.lower(), pattern.strip().lower())


def find_rule(rules, filename: str):
    """The first rule whose pattern matches. `rules` holds objects or dicts with a pattern. Returns (index, rule) or (None, None)."""
    for index, rule in enumerate(rules):
        pattern = rule["pattern"] if isinstance(rule, dict) else rule.pattern
        if matches(pattern, filename):
            return index, rule
    return None, None


def resolve_rule(profile: DirectoryProfile, filename: str):
    """(mapping, rule) for a file of this profile.
    (None, None): the profile has no enabled mapping.  (mapping, None): it has one but no rule matches the file name."""
    mapping = Mapping.objects.filter(profile=profile, enabled=True).select_related("connection").first()
    if mapping is None:
        return None, None
    _, rule = find_rule(list(mapping.rules.all()), filename)
    return mapping, rule


def resolve_target(profile: DirectoryProfile, filename: str):
    """(connection, table) for a file of this profile, or None when there is no enabled mapping or no rule matches."""
    mapping, rule = resolve_rule(profile, filename)
    return (mapping.connection, rule.table) if rule else None


def check_pattern(raw) -> tuple[str, str]:
    """Returns (clean pattern, error)."""
    if not isinstance(raw, str) or not raw.strip():
        return "", "Enter a file name pattern, for example emp*.*"
    text = raw.strip()
    if len(text) > 120:
        return text, "Use 120 characters or fewer."
    if "/" in text or "\\" in text:
        return text, "A pattern matches the file name only, so it can’t contain a folder."
    return text, ""


def check_table(raw) -> tuple[str, str]:
    """Accepts table, schema.table or database.schema.table."""
    if not isinstance(raw, str) or not raw.strip():
        return "", "Enter the table, for example dbo.EMP"
    text = raw.strip()
    parts = text.split(".")
    if len(parts) > 3 or not all(_PART.match(part) and len(part) <= 128 for part in parts):
        return text, "Use letters, digits and _ only, as table, schema.table or database.schema.table (for example dbo.EMP)."
    return text, ""


def check_keys(raw) -> tuple[str, str]:
    """Merge key columns: 'id' or 'a, b'. Returns (clean text, error)."""
    if raw is None or raw == "":
        return "", ""
    if not isinstance(raw, str):
        return "", "Enter column names separated by commas, for example id"
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if len(names) > 10 or not all(_PART.match(n) and len(n) <= 128 for n in names):
        return raw, "Use column names (letters, digits and _ only) separated by commas, for example id or dept, empno."
    if len({n.lower() for n in names}) != len(names):
        return raw, "A column is listed twice."
    return ", ".join(names), ""


def key_list(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def validate(payload: dict, instance: Mapping | None = None):
    """Returns (clean, errors). Saves nothing. For updates, keys that are missing keep the saved value.
    Error keys are field names; rule problems use rules.<n>.pattern and rules.<n>.table (n counts from 0)."""
    errors: dict[str, str] = {}
    for key in payload:
        if key not in FIELDS:
            errors[key] = "Unknown field."

    # profile: one mapping per profile, and it can't change after the mapping exists
    profile = instance.profile if instance else None
    if instance is None or "profile" in payload:
        raw = payload.get("profile")
        found = DirectoryProfile.objects.filter(pk=raw).first() if isinstance(raw, int) and not isinstance(raw, bool) else None
        if found is None:
            errors["profile"] = "Choose a directory profile."
        elif instance is not None and found.pk != instance.profile_id:
            errors["profile"] = "The profile of a mapping can’t be changed. Delete the mapping and add a new one."
        elif instance is None and Mapping.objects.filter(profile=found).exists():
            errors["profile"] = f"Profile “{found.name}” already has a mapping."
        else:
            profile = found

    connection = instance.connection if instance else None
    if instance is None or "connection" in payload:
        raw = payload.get("connection")
        found = DatabaseConnection.objects.filter(pk=raw).first() if isinstance(raw, int) and not isinstance(raw, bool) else None
        if found is None:
            errors["connection"] = "Choose a database connection."
        else:
            connection = found

    enabled = instance.enabled if instance else True
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            errors["enabled"] = "Must be on or off."
        else:
            enabled = payload["enabled"]

    if "rules" in payload or instance is None:
        raw_rules = payload.get("rules", [])
    else:
        raw_rules = [{"pattern": r.pattern, "table": r.table, "create_table": r.create_table, "merge_keys": r.merge_keys} for r in instance.rules.all()]
    rules = []
    if not isinstance(raw_rules, list):
        errors["rules"] = "Rules must be a list."
    elif not raw_rules:
        errors["rules"] = "Add at least one rule, for example emp*.* => dbo.EMP"
    elif len(raw_rules) > MAX_RULES:
        errors["rules"] = f"Use {MAX_RULES} rules or fewer."
    else:
        seen: dict[str, int] = {}
        for n, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                errors[f"rules.{n}.pattern"] = "Each rule needs a pattern and a table."
                continue
            pattern, bad = check_pattern(raw.get("pattern"))
            if bad:
                errors[f"rules.{n}.pattern"] = bad
            elif pattern.lower() in seen:
                errors[f"rules.{n}.pattern"] = f"Same pattern as rule {seen[pattern.lower()] + 1}, so this rule would never be used."
            else:
                seen[pattern.lower()] = n
            table, bad = check_table(raw.get("table"))
            if bad:
                errors[f"rules.{n}.table"] = bad
            create = raw.get("create_table", False)
            if not isinstance(create, bool):
                errors[f"rules.{n}.create_table"] = "Must be on or off."
            keys, bad = check_keys(raw.get("merge_keys", ""))
            if bad:
                errors[f"rules.{n}.merge_keys"] = bad
            rules.append({"pattern": pattern, "table": table, "create_table": create is True, "merge_keys": keys})

    if errors:
        return {}, errors
    return {"profile": profile, "connection": connection, "enabled": enabled, "rules": rules}, {}


@transaction.atomic
def save(clean: dict, instance: Mapping | None = None) -> Mapping:
    if instance is None:
        instance = Mapping(profile=clean["profile"])
    instance.connection = clean["connection"]
    instance.enabled = clean["enabled"]
    instance.save()
    instance.rules.all().delete()
    MappingRule.objects.bulk_create(
        MappingRule(mapping=instance, position=n, pattern=r["pattern"], table=r["table"], create_table=r["create_table"], merge_keys=r["merge_keys"]) for n, r in enumerate(clean["rules"])
    )
    return instance


def to_dict(mapping: Mapping) -> dict:
    return {
        "id": mapping.pk,
        "profile": mapping.profile_id,
        "profile_name": mapping.profile.name,
        "connection": mapping.connection_id,
        "connection_name": mapping.connection.name,
        "connection_kind": mapping.connection.kind,
        "enabled": mapping.enabled,
        "rules": [{"pattern": r.pattern, "table": r.table, "create_table": r.create_table, "merge_keys": r.merge_keys} for r in mapping.rules.all()],
    }


def find_table(tables: list[dict], name: str) -> list[dict]:
    """Which of the connection's tables does `name` (table, schema.table or database.schema.table) refer to?
    Case does not matter. Without a schema, a table of that name in any schema counts."""
    parts = name.strip().split(".")
    table = parts[-1].lower()
    schema = parts[-2].lower() if len(parts) >= 2 else None
    return [t for t in tables if t["table"].lower() == table and (schema is None or t["schema"].lower() == schema)]


def verify_tables(tables: list[dict], names: list[str]) -> list[dict]:
    out = []
    for name in names:
        found = find_table(tables, name) if isinstance(name, str) and name.strip() else []
        out.append(
            {
                "name": name,
                "exists": bool(found),
                "found": [f"{t['schema']}.{t['table']}" if t["schema"] else t["table"] for t in found[:5]],
            }
        )
    return out


def try_names(rules: list[dict], filenames: list[str]) -> list[dict]:
    """For the 'try it' box: which rule would each file name use."""
    out = []
    for name in filenames:
        index, rule = find_rule(rules, name)
        out.append({"filename": name, "rule": index, "pattern": rule["pattern"] if rule else None, "table": rule.get("table") if rule else None})
    return out
