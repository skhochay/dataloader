"""Open a real connection to a configured database and report whether it works.

Each driver is imported only when used, so you install just the ones you need.
"""
import importlib
import sqlite3
import ssl
import threading
import time
from pathlib import Path

from .db_kinds import KINDS, driver_available


# ---- ODBC connection string ---------------------------------------------------
def _odbc_value(value: str) -> str:
    """Quote a value when it contains characters that would break the string."""
    if any(ch in value for ch in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def odbc_connection_string(cfg: dict, password: str) -> str:
    options = cfg.get("options") or {}
    parts = []
    if options.get("dsn"):
        parts.append(f"DSN={_odbc_value(options['dsn'])}")
    if options.get("driver"):
        parts.append("DRIVER={" + options["driver"].replace("}", "}}") + "}")
    if cfg.get("host"):
        server = cfg["host"] + (f",{cfg['port']}" if cfg.get("port") else "")
        parts.append(f"SERVER={_odbc_value(server)}")
    if cfg.get("database"):
        parts.append(f"DATABASE={_odbc_value(cfg['database'])}")
    if cfg.get("username"):
        parts.append(f"UID={_odbc_value(cfg['username'])}")
    if password:
        parts.append(f"PWD={_odbc_value(password)}")
    extra = (options.get("extra") or "").strip().strip(";")
    if extra:
        parts.append(extra)
    return ";".join(parts) + ";"


# ---- one connect function per database ------------------------------------------
def _connect_oracle(cfg, password, timeout):
    oracledb = importlib.import_module("oracledb")
    if (cfg.get("options") or {}).get("connect_as") == "sid":
        dsn = oracledb.makedsn(cfg["host"], cfg["port"], sid=cfg["database"])
    else:
        dsn = oracledb.makedsn(cfg["host"], cfg["port"], service_name=cfg["database"])
    return oracledb.connect(user=cfg["username"], password=password, dsn=dsn, tcp_connect_timeout=timeout)


def _connect_sqlserver(cfg, password, timeout):
    pymssql = importlib.import_module("pymssql")
    kwargs = dict(
        server=cfg["host"],
        user=cfg["username"],
        password=password,
        database=cfg.get("database") or "",
        login_timeout=timeout,
        timeout=cfg.get("io_timeout", timeout) or 0,  # 0 = no limit (a load can take a while)
    )
    if cfg.get("port"):  # a named instance (host\\instance) is found through SQL Browser, so no port
        kwargs["port"] = str(cfg["port"])
    return pymssql.connect(**kwargs)


def _connect_vertica(cfg, password, timeout):
    vertica_python = importlib.import_module("vertica_python")
    return vertica_python.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["username"],
        password=password,
        database=cfg["database"],
        connection_timeout=timeout,
        tlsmode=(cfg.get("options") or {}).get("tlsmode", "prefer"),
    )


def _connect_postgres(cfg, password, timeout):
    psycopg = importlib.import_module("psycopg")
    return psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["username"],
        password=password or None,
        connect_timeout=timeout,
        sslmode=(cfg.get("options") or {}).get("sslmode", "prefer"),
    )


def _connect_mysql(cfg, password, timeout):
    pymysql = importlib.import_module("pymysql")
    kwargs = dict(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["username"],
        password=password,
        database=cfg.get("database") or None,
        connect_timeout=timeout,
        read_timeout=cfg.get("io_timeout", timeout) or None,
        write_timeout=cfg.get("io_timeout", timeout) or None,
    )
    if (cfg.get("options") or {}).get("use_ssl"):
        kwargs["ssl"] = ssl.create_default_context()
    return pymysql.connect(**kwargs)


def _connect_sqlite(cfg, password, timeout):
    path = Path(cfg["database"])
    if not path.is_file():
        raise FileNotFoundError(f"No file at {path}")
    if cfg.get("write"):  # loading: read-write, but never creates a new file by accident
        return sqlite3.connect(str(path), timeout=timeout)
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)  # read-only


def _connect_odbc(cfg, password, timeout):
    pyodbc = importlib.import_module("pyodbc")
    return pyodbc.connect(odbc_connection_string(cfg, password), timeout=cfg.get("io_timeout", timeout) or 0)


CONNECTORS = {
    "oracle": _connect_oracle,
    "sqlserver": _connect_sqlserver,
    "vertica": _connect_vertica,
    "postgres": _connect_postgres,
    "mysql": _connect_mysql,
    "sqlite": _connect_sqlite,
    "odbc": _connect_odbc,
}

VERSION_SQL = {
    "postgres": "SELECT version()",
    "mysql": "SELECT VERSION()",
    "sqlserver": "SELECT @@VERSION",
    "vertica": "SELECT version()",
    "sqlite": "SELECT sqlite_version()",
}

# SQLite's "SELECT 1" never reads the file, so read the schema to prove it really is a database.
PROBE_SQL = {"oracle": "SELECT 1 FROM DUAL", "sqlite": "SELECT count(*) FROM sqlite_master"}


# ---- the test ---------------------------------------------------------------------
def _server_version(kind, conn, cursor) -> str:
    try:
        if kind == "oracle":
            text = str(conn.version)
        elif kind == "odbc":
            pyodbc = importlib.import_module("pyodbc")
            text = f"{conn.getinfo(pyodbc.SQL_DBMS_NAME)} {conn.getinfo(pyodbc.SQL_DBMS_VER)}"
        else:
            cursor.execute(VERSION_SQL[kind])
            row = cursor.fetchone()
            text = str(row[0]) if row else ""
    except Exception:  # the version is a nicety; the connection itself already worked
        return ""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return " ".join(first_line.split())[:120]


def _probe(kind, conn) -> str:
    cursor = conn.cursor()
    try:
        cursor.execute(PROBE_SQL.get(kind, "SELECT 1"))
        cursor.fetchone()
        return _server_version(kind, conn, cursor)
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _hint(kind, text: str) -> str:
    """A plain-language next step for the failures people hit most often."""
    lowered = text.lower()
    if kind == "sqlserver":
        if "20009" in text or "tds server is unavailable" in lowered:
            return (
                "Nothing answered at that host and port. Check that SQL Server is running and that TCP/IP is "
                "enabled in SQL Server Configuration Manager. For a named instance such as SQLEXPRESS, enter "
                "host\\instance (for example PC1\\SQLEXPRESS) and leave the port blank."
            )
        if "18456" in text or "login failed" in lowered:
            return (
                "The server answered but rejected the login. Check the username and password, and that the "
                "server allows SQL Server authentication. For Windows authentication, use the ODBC type with "
                "Trusted_Connection=yes; in Extra attributes."
            )
    if "timed out" in lowered or "timeout" in lowered:
        return "The server didn’t answer in time. Check the host name, the port and any firewall in between."
    return ""


def _describe_error(exc: Exception, password: str, kind=None) -> str:
    text = f"{type(exc).__name__}: {str(exc).strip() or 'no details from the driver'}"
    text = text[:600]
    hint = _hint(kind, text)
    if hint:
        text += "\n\n" + hint
    if password:
        text = text.replace(password, "***")  # never echo a password back
    return text


def test_connection(cfg: dict, password: str = "", timeout: int = 10) -> dict:
    """cfg: kind, host, port, database, username, options. Returns {ok, message, server_version, elapsed_ms}."""
    spec = KINDS[cfg["kind"]]
    if not driver_available(spec):
        return {
            "ok": False,
            "message": f"The {spec['label']} driver isn’t installed on the server. Run: pip install {spec['package']}",
        }

    port = cfg.get("port") or spec["default_port"]
    if cfg["kind"] == "sqlserver" and "\\" in (cfg.get("host") or "") and not cfg.get("port"):
        port = None  # named instance: let SQL Browser find the port
    cfg = {**cfg, "port": port}
    outcome = {}

    def work():
        started = time.monotonic()
        try:
            conn = CONNECTORS[cfg["kind"]](cfg, password, timeout)
            try:
                version = _probe(cfg["kind"], conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            elapsed = int((time.monotonic() - started) * 1000)
            message = f"Connected in {elapsed} ms."
            if version:
                message += f" Server: {version}"
            outcome.update(ok=True, message=message, server_version=version, elapsed_ms=elapsed)
        except Exception as exc:
            outcome.update(ok=False, message=_describe_error(exc, password, cfg["kind"]))

    hard_limit = timeout + 5  # some drivers ignore their own timeouts (DNS stalls, firewalls)
    worker = threading.Thread(target=work, daemon=True, name="db-test")
    worker.start()
    worker.join(hard_limit)
    if worker.is_alive():
        return {"ok": False, "message": f"No answer after {hard_limit} seconds. Check the host, port and firewall."}
    return outcome


# ---- browsing tables ---------------------------------------------------------------
MAX_TABLES = 5000
_ORACLE_SYSTEM_OWNERS = (
    "SYS", "SYSTEM", "XDB", "CTXSYS", "MDSYS", "ORDSYS", "ORDDATA", "OUTLN", "DBSNMP", "APPQOSSYS", "WMSYS", "LBACSYS",
    "DVSYS", "GSMADMIN_INTERNAL", "AUDSYS", "OJVMSYS", "OLAPSYS", "ORACLE_OCM", "SI_INFORMTN_SCHEMA", "SYSMAN", "EXFSYS",
    "DIP", "ANONYMOUS", "REMOTE_SCHEDULER_AGENT", "DBSFWUSER", "GGSYS", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC",
)
TABLE_SQL = {
    "oracle": "SELECT owner, table_name FROM all_tables WHERE owner NOT IN ("
    + ", ".join(f"'{o}'" for o in _ORACLE_SYSTEM_OWNERS)
    + ") AND table_name NOT LIKE 'BIN$%' AND nested = 'NO' ORDER BY owner, table_name",
    "sqlserver": "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY 1, 2",
    "postgres": "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
    "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
    "vertica": "SELECT table_schema, table_name FROM v_catalog.tables WHERE is_system_table = false ORDER BY 1, 2",
    "mysql": "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
    "AND table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys') ORDER BY 1, 2",
    "sqlite": "SELECT 'main', name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
}


def _read_tables(kind, conn) -> list[dict]:
    cursor = conn.cursor()
    try:
        if kind == "odbc":  # every ODBC driver answers this catalog call, whatever the database behind it
            cursor.tables(tableType="TABLE")
            rows = [(r[1] or "", r[2]) for r in cursor.fetchmany(MAX_TABLES + 1)]
        else:
            cursor.execute(TABLE_SQL[kind])
            rows = [(r[0] or "", r[1]) for r in cursor.fetchmany(MAX_TABLES + 1)]
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return [{"schema": str(s), "table": str(t)} for s, t in rows]


def connect_for_load(cfg: dict, password: str = "", timeout: int = 30):
    """A read-write connection for loading data (transaction control is up to the caller).
    Raises RuntimeError when the driver isn't installed; driver errors from connecting pass through."""
    spec = KINDS[cfg["kind"]]
    if not driver_available(spec):
        raise RuntimeError(f"The {spec['label']} driver isn’t installed on the server. Run: pip install {spec['package']}")
    port = cfg.get("port") or spec["default_port"]
    if cfg["kind"] == "sqlserver" and "\\" in (cfg.get("host") or "") and not cfg.get("port"):
        port = None
    return CONNECTORS[cfg["kind"]]({**cfg, "port": port, "write": True, "io_timeout": 0}, password, timeout)


def list_tables(cfg: dict, password: str = "", timeout: int = 15) -> dict:
    """The tables a connection can see. Returns {ok, tables: [{schema, table}], truncated} or {ok: False, message}."""
    spec = KINDS[cfg["kind"]]
    if not driver_available(spec):
        return {"ok": False, "message": f"The {spec['label']} driver isn’t installed on the server. Run: pip install {spec['package']}"}
    port = cfg.get("port") or spec["default_port"]
    if cfg["kind"] == "sqlserver" and "\\" in (cfg.get("host") or "") and not cfg.get("port"):
        port = None
    cfg = {**cfg, "port": port}
    outcome = {}

    def work():
        try:
            conn = CONNECTORS[cfg["kind"]](cfg, password, timeout)
            try:
                tables = _read_tables(cfg["kind"], conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            outcome.update(ok=True, tables=tables[:MAX_TABLES], truncated=len(tables) > MAX_TABLES)
        except Exception as exc:
            outcome.update(ok=False, message=_describe_error(exc, password, cfg["kind"]))

    worker = threading.Thread(target=work, daemon=True, name="db-tables")
    worker.start()
    worker.join(timeout + 5)
    if worker.is_alive():
        return {"ok": False, "message": f"No answer after {timeout + 5} seconds. Check the host, port and firewall."}
    return outcome
