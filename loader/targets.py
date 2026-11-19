"""Loading a file into the database and table its mapping rule names.

    emp001.json  --rule "emp*.* => dbo.EMP"-->  connection ORACLE_CONNECT, table dbo.EMP

How a load runs (load_file):

    1. Read the file once to learn its columns, and (only when the rule's "create table" tick is on and the
       table is missing) the type of each column. A bad file is rejected here, before the database is touched.
    2. Connect to the mapped database and look the table up in its catalog.
         - table exists                      -> the file's columns are matched to its columns by name (case does not matter)
         - missing, tick on                  -> CREATE TABLE from the types found in step 1, committed at once
         - missing, tick off                 -> the file fails with a message saying which table is missing
    3. Read the file again and insert it in batches inside ONE transaction on the target. With merge key columns on
       the rule, rows whose key exists are updated and the rest added (see write_plan for each database's statement).
    4. Just before the commit, `before_commit` checks this worker still owns the job. Then COMMIT.
       Any error before that rolls everything back, so a failed file leaves no partial rows.

Limits worth knowing (also in the README):
    * Oracle and MySQL commit DDL by themselves, so a table created for a file that then fails stays (empty).
    * Created tables get no indexes (a primary key only when the rule names merge key columns) and are never altered afterwards.
    * A crash in the instant between the target's COMMIT and marking the job LOADED loads the file twice
      (the job is retried). Use a table with a unique key if that matters to you.
    * Dates are read as ISO text (2024-1231 style is not understood): yyyy-mm-dd and yyyy-mm-dd[T ]hh:mm[:ss[.ffffff]][Z|+hh:mm].
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import db_connect, vault
from .parsers import ParseError

log = logging.getLogger("loader.targets")


class TargetError(Exception):
    """The file can't be loaded into the mapped table as things stand (no rule, no such table, unknown columns ...).
    Retrying won't help until someone changes the mapping, the table or the file."""


class NoMappingRuleError(TargetError):
    """The profile has a mapping but none of its rules matches the file name."""


class TargetDatabaseError(Exception):
    """The target database could not be reached or dropped the connection. Worth retrying."""


# ---- dialects ---------------------------------------------------------------
class Dialect:
    def __init__(self, name, *, quote='"', fold=None, param="?", max_ident=128, bool_native=False, types=None, merge=None):
        self.name = name
        self.merge = merge  # how rows with an existing key are updated: tsql | oracle | on_conflict | dup_key | None (not supported)
        self.quote_open, self.quote_close = (quote, quote) if len(quote) == 1 else (quote[0], quote[1])
        self.fold = fold or (lambda s: s)
        self.param = param
        self.max_ident = max_ident
        self.bool_native = bool_native
        self.types = types

    def q(self, name: str) -> str:
        return self.quote_open + name.replace(self.quote_close, self.quote_close * 2) + self.quote_close

    def marker(self, n: int) -> str:
        return f":{n}" if self.param == ":n" else self.param

    def text_type(self, length: int) -> str:
        """`length` is the longest value seen; leave room for longer ones in later files."""
        t = self.types
        want = length * 2
        for size in (255, 1000, 4000):
            if want <= size and size <= t.get("text_max", 4000):
                return t["text"].format(n=size)
        return t["clob"]


_STD = {"int": "BIGINT", "float": "DOUBLE PRECISION", "bool": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP",
        "text": "VARCHAR({n})", "clob": "TEXT", "text_max": 4000}

DIALECTS = {
    "oracle": Dialect("oracle", fold=str.upper, param=":n", max_ident=30, merge="oracle",
                      types={"int": "NUMBER(19)", "float": "BINARY_DOUBLE", "bool": "NUMBER(1)", "date": "DATE", "datetime": "TIMESTAMP",
                             "text": "VARCHAR2({n} CHAR)", "clob": "CLOB", "text_max": 1000}),
    "sqlserver": Dialect("sqlserver", quote="[]", param="%s", max_ident=128, merge="tsql",
                         types={"int": "BIGINT", "float": "FLOAT", "bool": "BIT", "date": "DATE", "datetime": "DATETIME2",
                                "text": "NVARCHAR({n})", "clob": "NVARCHAR(MAX)", "text_max": 4000}),
    "postgres": Dialect("postgres", fold=str.lower, param="%s", max_ident=63, bool_native=True, types=_STD, merge="on_conflict"),
    "mysql": Dialect("mysql", quote="``", param="%s", max_ident=64, merge="dup_key",
                     types={"int": "BIGINT", "float": "DOUBLE", "bool": "TINYINT(1)", "date": "DATE", "datetime": "DATETIME(6)",
                            "text": "VARCHAR({n})", "clob": "LONGTEXT", "text_max": 1000}),
    "vertica": Dialect("vertica", param="%s", max_ident=128, bool_native=True,
                       types={**_STD, "int": "INT", "float": "FLOAT", "clob": "VARCHAR(65000)"}),
    "sqlite": Dialect("sqlite", param="?", max_ident=1000, merge="on_conflict",
                      types={"int": "INTEGER", "float": "REAL", "bool": "INTEGER", "date": "TEXT", "datetime": "TEXT",
                             "text": "TEXT", "clob": "TEXT", "text_max": 4000}),
    "odbc": Dialect("odbc", param="?", max_ident=64, types={**_STD, "clob": "VARCHAR(8000)"}),
}
# An ODBC data source can sit in front of any database; use that database's rules once the driver says which it is.
ODBC_BY_DBMS = {"microsoft sql server": "sqlserver", "oracle": "oracle", "postgresql": "postgres", "mysql": "mysql", "vertica": "vertica"}


def dialect_for(kind: str, conn) -> Dialect:
    if kind != "odbc":
        return DIALECTS[kind]
    try:
        import pyodbc

        dbms = str(conn.getinfo(pyodbc.SQL_DBMS_NAME)).lower()
    except Exception:
        return DIALECTS["odbc"]
    for key, name in ODBC_BY_DBMS.items():
        if key in dbms:
            base = DIALECTS[name]
            return Dialect(name, quote=base.quote_open + base.quote_close, fold=base.fold, param="?", max_ident=base.max_ident,
                           bool_native=False, types=base.types, merge=base.merge)
    return DIALECTS["odbc"]


# ---- table names -----------------------------------------------------------------
def split_table(name: str) -> tuple[str | None, str | None, str]:
    """'dbo.EMP' -> (None, 'dbo', 'EMP'); 'db.dbo.EMP' -> ('db', 'dbo', 'EMP'); 'EMP' -> (None, None, 'EMP')."""
    parts = name.strip().split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0]


def table_ref(d: Dialect, db, schema, table) -> str:
    return ".".join(d.q(d.fold(p)) for p in (db, schema, table) if p)


# ---- reading the catalog ---------------------------------------------------------
def fetch_columns(d: Dialect, kind: str, conn, db, schema, table) -> list[tuple[str, str]]:
    """[(column name, database type)] of the table, in table order; [] when it doesn't exist."""
    cur = conn.cursor()
    try:
        t, s = d.fold(table), d.fold(schema) if schema else None
        if kind == "odbc":
            seen = []
            for tbl, sch in ((table, schema), (t, s), (table.lower(), schema.lower() if schema else None), (table.upper(), schema.upper() if schema else None)):
                if (tbl, sch) in seen:
                    continue
                seen.append((tbl, sch))
                kwargs = {"table": tbl}
                if sch:
                    kwargs["schema"] = sch
                if db:
                    kwargs["catalog"] = db
                cur.columns(**kwargs)
                rows = [(str(r[3]), str(r[5])) for r in cur.fetchall()]  # COLUMN_NAME, TYPE_NAME
                if rows:
                    return rows
            return []
        if d.name == "sqlite":
            if schema and schema.lower() != "main":
                raise TargetError(f"SQLite has no schema “{schema}”. Use just the table name, or main.{table}.")
            cur.execute("SELECT name, type FROM pragma_table_info(?)", (table,))
        elif d.name == "oracle":
            owner = "owner = " + (d.marker(1) if s else "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')")
            n = 2 if s else 1
            cur.execute(f"SELECT column_name, data_type FROM all_tab_columns WHERE {owner} AND table_name = {d.marker(n)} ORDER BY column_id",
                        ([s] if s else []) + [t])
        elif d.name == "sqlserver":
            cat = f"{d.q(db)}." if db else ""
            where = "TABLE_SCHEMA = " + (d.marker(1) if s else "SCHEMA_NAME()")
            cur.execute(f"SELECT COLUMN_NAME, DATA_TYPE FROM {cat}INFORMATION_SCHEMA.COLUMNS WHERE {where} AND TABLE_NAME = {d.marker(2 if s else 1)} ORDER BY ORDINAL_POSITION",
                        ([s] if s else []) + [t])
        else:  # postgres, mysql, vertica
            if db:
                raise TargetError(f"{kind} tables are named schema.table; “{db}.{schema}.{table}” has one part too many.")
            view = "v_catalog.columns" if d.name == "vertica" else "information_schema.columns"
            default = {"postgres": "current_schema()", "mysql": "DATABASE()", "vertica": "current_schema()"}[d.name]
            lower = d.name == "vertica"  # Vertica keeps the case a name was created with but compares case-insensitively
            sch_cond = f"{'lower(table_schema)' if lower else 'table_schema'} = " + (d.marker(1) if s else default)
            tbl_cond = f"{'lower(table_name)' if lower else 'table_name'} = {d.marker(2 if s else 1)}"
            args = ([s.lower() if lower else s] if s else []) + [t.lower() if lower else t]
            cur.execute(f"SELECT column_name, data_type FROM {view} WHERE {sch_cond} AND {tbl_cond} ORDER BY ordinal_position", args)
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        _quiet_close(cur)


def _quiet_close(obj):
    try:
        obj.close()
    except Exception:
        pass


# ---- values and types -------------------------------------------------------------
_INT = re.compile(r"^-?(0|[1-9]\d{0,17})$")  # leading zeros stay text (zip codes, codes); 18 digits always fit a BIGINT
_FLOAT = re.compile(r"^-?((0|[1-9]\d*)\.\d*|\.\d+|(0|[1-9]\d*)(?=[eE]))([eE][+-]?\d+)?$")  # needs a point or an exponent; 007 is not a number
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_temporal(text: str):
    """A date, a datetime (timezone converted to UTC and dropped), or None when the text isn't ISO."""
    text = text.strip()
    try:
        if _DATE.match(text):
            return date.fromisoformat(text)
        if _DATETIME.match(text):
            clean = re.sub(r"(\.\d{6})\d+", r"\1", text.replace(" ", "T", 1)).replace(" ", "")
            clean = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
            value = datetime.fromisoformat(clean)
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
    except ValueError:
        return None
    return None


def kind_of(value):
    """int | float | bool | date | datetime | text, or None for an empty value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if abs(value) < 2**63 else "text"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _INT.match(text):
            return "int"
        if _FLOAT.match(text):
            return "float"
        parsed = parse_temporal(text)
        if isinstance(parsed, datetime):
            return "datetime"
        if parsed is not None:
            return "date"
        return "text"
    return "text"


def merge_kinds(kinds: set) -> str:
    kinds = set(kinds)
    if not kinds:
        return "text"
    if len(kinds) == 1:
        return next(iter(kinds))
    if kinds <= {"int", "float"}:
        return "float"
    if kinds <= {"date", "datetime"}:
        return "datetime"
    return "text"


def as_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def kind_from_db_type(db_type: str):
    """What our value conversion should do for a column that already exists."""
    t = (db_type or "").lower()
    if "timestamp" in t or "datetime" in t:
        return "datetime"
    if t == "date":
        return "date"
    if t in ("bit", "bool", "boolean"):
        return "bool"
    if any(x in t for x in ("char", "text", "clob", "string", "json", "xml", "uuid", "uniqueidentifier", "enum", "varchar")):
        return "text"
    if "int" in t or t in ("number", "numeric", "decimal", "float", "double", "real", "money", "smallmoney", "double precision", "binary_double", "binary_float", "dec"):
        return "num"
    return None  # blob, geometry ... pass the value through and let the database decide


class RowError(TargetError):
    pass


def coerce(value, kind, d: Dialect):
    """Convert a parsed value for a column of this kind. Empty text becomes NULL for every kind."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if kind == "text":
        return as_text(value)
    if kind is None:
        return as_text(value) if isinstance(value, (dict, list)) else value
    if kind == "bool":
        if isinstance(value, bool):
            flag = value
        elif isinstance(value, (int, float)) and value in (0, 1):
            flag = bool(value)
        elif isinstance(value, str) and value.strip().lower() in ("true", "t", "yes", "y", "1"):
            flag = True
        elif isinstance(value, str) and value.strip().lower() in ("false", "f", "no", "n", "0"):
            flag = False
        else:
            raise RowError(f"“{value}” is not a true/false value")
        return flag if d.bool_native else int(flag)
    if kind in ("int", "num"):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, Decimal)):
            return value
        text = str(value).strip()
        if _INT.match(text):
            return int(text)
        if text.lower() in ("true", "false"):  # a flag stored in a number column (NUMBER(1), TINYINT ...)
            return int(text.lower() == "true")
        try:
            return Decimal(text) if kind == "num" else float(text)
        except (InvalidOperation, ValueError):
            raise RowError(f"“{value}” is not a number") from None
    if kind == "float":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except ValueError:
            raise RowError(f"“{value}” is not a number") from None
    if kind in ("date", "datetime"):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = value
        else:
            parsed = parse_temporal(str(value))
            if parsed is None:
                raise RowError(f"“{value}” is not a date. Dates must look like 2024-12-31 or 2024-12-31 14:30:00")
        if kind == "datetime" and not isinstance(parsed, datetime):
            return datetime(parsed.year, parsed.month, parsed.day)
        if kind == "date" and isinstance(parsed, datetime) and parsed.time() == datetime.min.time():
            return parsed.date()
        return parsed
    return value


# ---- column names --------------------------------------------------------------------
_BAD = re.compile(r"[^A-Za-z0-9_$#@]")


def clean_name(raw: str, d: Dialect, taken: set) -> str:
    """A safe column name from a file's field name, unique among `taken` (case-insensitive)."""
    name = _BAD.sub("_", str(raw).strip()) or "column"
    if not re.match(r"[A-Za-z_]", name):
        name = "c_" + name
    name = d.fold(name)[: d.max_ident]
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        suffix = f"_{n}"
        candidate = name[: d.max_ident - len(suffix)] + suffix
    taken.add(candidate.lower())
    return candidate


# ---- reading the file ---------------------------------------------------------------------
class Shape:
    """What the first pass learns: the file's field names (in order of first appearance), the kinds seen in each, and text lengths."""

    def __init__(self):
        self.fields: dict[str, set] = {}
        self.longest: dict[str, int] = {}
        self.rows = 0

    def add(self, data: dict):
        self.rows += 1
        for key, value in data.items():
            kinds = self.fields.setdefault(key, set())
            kind = kind_of(value)
            if kind is not None:
                kinds.add(kind)
                text_len = len(as_text(value))
                if text_len > self.longest.get(key, 0):
                    self.longest[key] = text_len


def survey(parser, path) -> Shape:
    shape = Shape()
    rows = parser(path)
    try:
        for _, data in rows:
            shape.add(data)
    finally:
        rows.close()
    if shape.rows == 0:
        raise ParseError("file contains no data rows")
    return shape


# ---- creating the table ------------------------------------------------------------------------
def create_sql(d: Dialect, ref: str, columns: list[tuple[str, str]], primary_key: list[str] | None = None) -> str:
    parts = [f"{d.q(name)} {sql_type}" for name, sql_type in columns]
    if primary_key:
        parts.append(f"PRIMARY KEY ({', '.join(d.q(c) for c in primary_key)})")
    body = ",\n  ".join(parts)
    return f"CREATE TABLE {ref} (\n  {body}\n)"


def plan_new_table(d: Dialect, shape: Shape, merge_keys=()):
    """Names, SQL types and conversion kinds for a table made from the file.
    Returns (columns, kinds, mapping field->column, key columns). Key columns are NOT NULL and form the primary key."""
    taken: set = set()
    columns, kinds, fields = [], {}, {}
    for key, seen in shape.fields.items():
        name = clean_name(key, d, taken)
        kind = merge_kinds(seen)
        sql = d.text_type(max(shape.longest.get(key, 0), 1)) if kind == "text" else d.types[kind]
        columns.append((name, sql))
        kinds[name] = kind
        fields[key] = name
    key_cols = resolve_keys(merge_keys, fields)
    columns = [(n, t + " NOT NULL" if n in key_cols else t) for n, t in columns]
    return columns, kinds, fields, key_cols


def resolve_keys(merge_keys, fields) -> list[str]:
    """The table columns that the rule's key names stand for. `fields` maps file field -> table column."""
    out = []
    for key in merge_keys:
        hit = next((col for f, col in fields.items() if str(f).strip().lower() == key.lower() or col.lower() == key.lower()), None)
        if hit is None:
            raise TargetError(
                f"merge key “{key}” is not a column of the file. The file has: "
                + ", ".join(str(f) for f in list(fields)[:30]) + (" …" if len(fields) > 30 else "")
            )
        out.append(hit)
    return out


# ---- the statements that write a batch --------------------------------------------------------------
STAGE = "#dl_stage"


class WritePlan:
    """What to run: `prepare` once, then per batch `insert` (executemany) followed by `after_batch`, and `cleanup` at the end."""

    def __init__(self, insert, prepare=(), after_batch=(), cleanup=()):
        self.insert, self.prepare, self.after_batch, self.cleanup = insert, list(prepare), list(after_batch), list(cleanup)


def write_plan(d: Dialect, ref: str, cols: list[str], keys: list[str]) -> WritePlan:
    """Plain INSERT, or (with key columns) an update-or-insert in the database's own style."""
    q, marks = d.q, [d.marker(i + 1) for i in range(len(cols))]
    column_list = ", ".join(q(c) for c in cols)
    insert = f"INSERT INTO {ref} ({column_list}) VALUES ({', '.join(marks)})"
    if not keys:
        return WritePlan(insert)
    rest = [c for c in cols if c not in keys]  # key columns are never updated
    if d.merge == "tsql":
        on = " AND ".join(f"t.{q(k)} = s.{q(k)}" for k in keys)
        matched = f"WHEN MATCHED THEN UPDATE SET {', '.join(f't.{q(c)} = s.{q(c)}' for c in rest)} " if rest else ""
        merge = (f"MERGE {ref} AS t USING {STAGE} AS s ON ({on}) {matched}"
                 f"WHEN NOT MATCHED THEN INSERT ({column_list}) VALUES ({', '.join('s.' + q(c) for c in cols)});")
        # The stage copies the target's column types. Selecting through a UNION keeps IDENTITY out of the copy.
        blank = f"SELECT {column_list} FROM {ref}"
        return WritePlan(
            f"INSERT INTO {STAGE} ({column_list}) VALUES ({', '.join(marks)})",
            prepare=[f"SELECT TOP 0 {column_list} INTO {STAGE} FROM ({blank} UNION ALL {blank}) AS x"],
            after_batch=[merge, f"DELETE FROM {STAGE}"],
            cleanup=[f"DROP TABLE {STAGE}"],
        )
    if d.merge == "oracle":
        source = ", ".join(f"{m} AS {q(c)}" for m, c in zip(marks, cols))
        on = " AND ".join(f"t.{q(k)} = s.{q(k)}" for k in keys)
        matched = f"WHEN MATCHED THEN UPDATE SET {', '.join(f't.{q(c)} = s.{q(c)}' for c in rest)} " if rest else ""
        return WritePlan(
            f"MERGE INTO {ref} t USING (SELECT {source} FROM dual) s ON ({on}) {matched}"
            f"WHEN NOT MATCHED THEN INSERT ({column_list}) VALUES ({', '.join('s.' + q(c) for c in cols)})"
        )
    if d.merge == "on_conflict":
        target = ", ".join(q(k) for k in keys)
        action = f"DO UPDATE SET {', '.join(f'{q(c)} = excluded.{q(c)}' for c in rest)}" if rest else "DO NOTHING"
        return WritePlan(f"{insert} ON CONFLICT ({target}) {action}")
    if d.merge == "dup_key":
        action = ", ".join(f"{q(c)} = VALUES({q(c)})" for c in rest) or f"{q(keys[0])} = {q(keys[0])}"
        return WritePlan(f"{insert} ON DUPLICATE KEY UPDATE {action}")
    raise TargetError(
        f"merging is not available for {d.name} databases yet. Clear the key columns on the mapping rule to add rows without merging."
    )


def match_columns(shape: Shape, existing: list[tuple[str, str]], d: Dialect):
    """Match each file field to a column of the existing table. Returns (field->column, column->kind); raises when fields don't fit."""
    by_lower = {name.lower(): name for name, _ in existing}
    types = {name: db_type for name, db_type in existing}
    fields, unknown = {}, []
    for key in shape.fields:
        hit = by_lower.get(str(key).strip().lower()) or by_lower.get(_BAD.sub("_", str(key).strip()).lower())
        if hit is None:
            unknown.append(str(key))
        else:
            fields[key] = hit
    if unknown:
        raise TargetError(
            "the file has columns the table doesn’t: " + ", ".join(unknown[:10]) + (" …" if len(unknown) > 10 else "")
            + ". Table columns: " + ", ".join(name for name, _ in existing[:30]) + (" …" if len(existing) > 30 else "")
        )
    clash = {}
    for key, col in fields.items():
        clash.setdefault(col, []).append(key)
    for col, keys in clash.items():
        if len(keys) > 1:
            raise TargetError(f"the file columns {', '.join(map(str, keys))} all match table column {col}")
    return fields, {col: kind_from_db_type(types[col]) for col in fields.values()}


# ---- the load itself ------------------------------------------------------------------------------
def _is_connection_problem(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(cls.__name__ in ("OperationalError", "InterfaceError") for cls in type(exc).__mro__)


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


def config_of(connection):
    """(cfg dict, password) of a saved DatabaseConnection."""
    password = vault.decrypt(connection.password_enc) if connection.password_enc else ""
    cfg = {
        "kind": connection.kind, "host": connection.host, "port": connection.port, "database": connection.database,
        "username": connection.username, "options": connection.options,
    }
    return cfg, password


def load_file(parser, path, connection, table: str, create_table: bool, batch_size: int = 1000, before_commit=None, merge_keys=()) -> int:
    """Load `path` into `table` of the saved `connection` (see the module docstring). Returns the number of rows loaded.

    With `merge_keys` (column names), rows whose key already exists in the table are updated and the others added (MERGE);
    without, rows are only added.

    Raises ParseError (bad file), TargetError (mapping/table/columns don't fit), TargetDatabaseError (worth retrying)
    or whatever `before_commit` raises (the worker passes a check that it still owns the job).
    """
    cfg, password = config_of(connection)
    kind = cfg["kind"]
    shape = survey(parser, path)
    db, schema, name = split_table(table)

    try:
        conn = db_connect.connect_for_load(cfg, password)
    except RuntimeError as exc:
        raise TargetError(str(exc)) from None
    except Exception as exc:
        message = f"could not connect to {connection.name}: {db_connect._describe_error(exc, password, kind)}"
        if (_is_connection_problem(exc) or isinstance(exc, OSError)) and not isinstance(exc, FileNotFoundError):
            raise TargetDatabaseError(message) from None
        raise TargetError(message) from None  # wrong password, wrong database name ... won't fix itself
    try:
        d = dialect_for(kind, conn)
        ref = table_ref(d, db, schema, name)
        first = last = 0
        try:
            existing = fetch_columns(d, kind, conn, db, schema, name)
            if merge_keys and d.merge is None:
                write_plan(d, ref, ["x"], ["x"])  # raises the "not available" message
            if existing:
                fields, kinds = match_columns(shape, existing, d)
                key_cols = resolve_keys(merge_keys, fields)
            elif not create_table:
                raise TargetError(
                    f"table {table} doesn’t exist in {connection.name}. Create it, or tick “create table if it doesn’t exist” on the mapping rule."
                )
            else:
                columns, kinds, fields, key_cols = plan_new_table(d, shape, merge_keys)
                try:
                    cur = conn.cursor()
                    cur.execute(create_sql(d, ref, columns, key_cols))
                    _quiet_close(cur)
                    conn.commit()  # same everywhere: Oracle and MySQL commit DDL anyway, so don't pretend it can roll back
                    log.info("Created table %s in %s (%s columns)", table, connection.name, len(columns))
                except Exception as exc:
                    _rollback(conn)
                    again = fetch_columns(d, kind, conn, db, schema, name)  # another worker may have created it first
                    if not again:
                        raise TargetError(f"could not create table {table}: {db_connect._describe_error(exc, password, kind)} "
                                          "(the schema must already exist; DataLoader creates tables, not schemas)") from None
                    fields, kinds = match_columns(shape, again, d)
                    key_cols = resolve_keys(merge_keys, fields)

            order = list(fields.items())  # [(file field, table column)]
            plan = write_plan(d, ref, [col for _, col in order], key_cols)
            key_at = [n for n, (_, col) in enumerate(order) if col in key_cols]
            cur = conn.cursor()
            for statement in plan.prepare:
                cur.execute(statement)
            if d.name == "oracle":
                # Oracle sizes its bind buffers from the first row; a NULL there would make later text "too long".
                sizes = [min(max(shape.longest.get(f, 0), 1) * 4, 32767) if kinds[c] == "text" else None for f, c in order]
                try:
                    cur.setinputsizes(*sizes)
                except Exception:
                    pass
            total, batch, seen = 0, [], {}
            rows = parser(path)
            try:
                for number, data in rows:
                    try:
                        row = tuple(coerce(data.get(f), kinds[c], d) for f, c in order)
                    except RowError as exc:
                        raise TargetError(f"row {number}: {exc}") from None
                    total += 1
                    if not batch:
                        first = number
                    last = number
                    if key_at:
                        key = tuple(row[n] for n in key_at)
                        if any(v is None for v in key):
                            empty = [order[n][1] for n in key_at if row[n] is None]
                            raise TargetError(f"row {number}: key column {', '.join(empty)} is empty")
                        if key in seen:  # the same key twice in one batch: the later row wins, as if merged in order
                            batch[seen[key]] = row
                            continue
                        seen[key] = len(batch)
                    batch.append(row)
                    if len(batch) >= batch_size:
                        _flush(cur, plan, batch, first, last)
                        batch, seen = [], {}
                if batch:
                    _flush(cur, plan, batch, first, last)
            finally:
                rows.close()
            for statement in plan.cleanup:
                cur.execute(statement)
            _quiet_close(cur)
            if before_commit is not None:
                before_commit()
            conn.commit()
            return total
        except (ParseError, TargetError, TargetDatabaseError):
            _rollback(conn)
            raise
        except Exception as exc:
            _rollback(conn)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if _is_connection_problem(exc) or isinstance(exc, OSError):
                raise TargetDatabaseError(f"lost the connection to {connection.name}: {db_connect._describe_error(exc, password, kind)}") from None
            if type(exc).__module__.startswith("loader."):
                raise
            raise TargetError(db_connect._describe_error(exc, password, kind)) from None
    finally:
        _quiet_close(conn)


def _flush(cur, plan, batch, first, last):
    try:
        cur.executemany(plan.insert, batch)
        for statement in plan.after_batch:
            cur.execute(statement)
    except Exception as exc:
        if _is_connection_problem(exc):
            raise
        raise TargetError(f"rows {first}–{last}: {db_connect._describe_error(exc, '', None)}") from None
