"""The database types a connection can point at, and what each one needs.

This table drives both validation on the server and the form in the browser.
To add a database: add an entry here and a _connect_<kind> function in db_connect.py.
"""
import importlib
import importlib.util


def _field(label, required=False, help=""):
    return {"label": label, "required": required, "help": help}


def _choices(pairs):
    return [{"value": value, "label": label} for value, label in pairs]


_TLS_MODES = _choices(
    [("disable", "Disable"), ("prefer", "Prefer"), ("require", "Require"), ("verify-ca", "Verify CA"), ("verify-full", "Verify full")]
)

# Order here is the order shown in the dropdown.
KINDS = {
    "oracle": {
        "label": "Oracle",
        "module": "oracledb",
        "package": "oracledb",
        "default_port": 1521,
        "fields": {
            "host": _field("Host", True),
            "port": _field("Port"),
            "database": _field("Service name or SID", True),
            "username": _field("Username", True),
            "password": _field("Password"),
        },
        "options": [
            {
                "key": "connect_as",
                "label": "The name above is a",
                "type": "select",
                "choices": _choices([("service_name", "Service name"), ("sid", "SID")]),
                "default": "service_name",
            }
        ],
    },
    "sqlserver": {
        "label": "SQL Server",
        "module": "pymssql",
        "package": "pymssql",
        "default_port": 1433,
        "fields": {
            "host": _field("Host", True, "For a named instance use host\\instance and leave the port blank."),
            "port": _field("Port"),
            "database": _field("Database", False, "Leave blank to use the login’s default database."),
            "username": _field("Username", True),
            "password": _field("Password"),
        },
        "options": [],
    },
    "vertica": {
        "label": "Vertica",
        "module": "vertica_python",
        "package": "vertica-python",
        "default_port": 5433,
        "fields": {
            "host": _field("Host", True),
            "port": _field("Port"),
            "database": _field("Database", True),
            "username": _field("Username", True),
            "password": _field("Password"),
        },
        "options": [
            {"key": "tlsmode", "label": "TLS mode", "type": "select", "choices": _TLS_MODES, "default": "prefer"}
        ],
    },
    "postgres": {
        "label": "PostgreSQL",
        "module": "psycopg",
        "package": "psycopg[binary]",
        "default_port": 5432,
        "fields": {
            "host": _field("Host", True),
            "port": _field("Port"),
            "database": _field("Database", True),
            "username": _field("Username", True),
            "password": _field("Password"),
        },
        "options": [
            {
                "key": "sslmode",
                "label": "SSL mode",
                "type": "select",
                "choices": _choices(
                    [("disable", "Disable"), ("allow", "Allow"), ("prefer", "Prefer"), ("require", "Require"),
                     ("verify-ca", "Verify CA"), ("verify-full", "Verify full")]
                ),
                "default": "prefer",
            }
        ],
    },
    "mysql": {
        "label": "MySQL",
        "module": "pymysql",
        "package": "pymysql",
        "default_port": 3306,
        "fields": {
            "host": _field("Host", True),
            "port": _field("Port"),
            "database": _field("Database", False, "Leave blank to connect without selecting one."),
            "username": _field("Username", True),
            "password": _field("Password"),
        },
        "options": [
            {"key": "use_ssl", "label": "Encrypt with TLS", "type": "bool", "default": False,
             "help": "Verifies the server certificate."}
        ],
    },
    "sqlite": {
        "label": "SQLite",
        "module": "sqlite3",
        "package": "",
        "default_port": None,
        "fields": {
            "database": _field("Database file", True, "Full path to an existing file, for example C:\\Data\\app.db"),
        },
        "options": [],
    },
    "odbc": {
        "label": "ODBC",
        "module": "pyodbc",
        "package": "pyodbc",
        "default_port": None,
        "fields": {
            "host": _field("Server", False, "Used with a driver name. Not needed when you use a DSN."),
            "port": _field("Port"),
            "database": _field("Database"),
            "username": _field("Username"),
            "password": _field("Password"),
        },
        "options": [
            {"key": "dsn", "label": "DSN", "type": "text", "default": "",
             "help": "A data source name set up in the Windows ODBC administrator."},
            {"key": "driver", "label": "Driver", "type": "text", "default": "",
             "help": "For example ODBC Driver 18 for SQL Server. Use this or a DSN."},
            {"key": "extra", "label": "Extra attributes", "type": "textarea", "default": "",
             "help": "Added to the connection string as written, for example TrustServerCertificate=yes;"},
        ],
    },
}


def driver_available(spec) -> bool:
    if spec["module"] == "sqlite3":
        return True
    importlib.invalidate_caches()  # notice a driver installed since the server started
    return importlib.util.find_spec(spec["module"]) is not None


def public_kinds() -> list:
    """What the browser needs to draw the form."""
    out = []
    for kind, spec in KINDS.items():
        out.append(
            {
                "kind": kind,
                "label": spec["label"],
                "default_port": spec["default_port"],
                "package": spec["package"],
                "available": driver_available(spec),
                "fields": [{"name": name, **field} for name, field in spec["fields"].items()],
                "options": spec["options"],
            }
        )
    return out
