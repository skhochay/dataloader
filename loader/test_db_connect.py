import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import db_connect
from .db_kinds import KINDS, public_kinds


class KindsTests(unittest.TestCase):
    def test_the_requested_databases_are_offered_in_order(self):
        self.assertEqual(
            [k["label"] for k in public_kinds()],
            ["Oracle", "SQL Server", "Vertica", "PostgreSQL", "MySQL", "SQLite", "ODBC"],
        )

    def test_every_kind_has_a_connector(self):
        self.assertEqual(set(KINDS), set(db_connect.CONNECTORS))


class OdbcStringTests(unittest.TestCase):
    def test_dsn_with_credentials(self):
        cfg = {"username": "app", "options": {"dsn": "Warehouse"}}
        self.assertEqual(db_connect.odbc_connection_string(cfg, "pw"), "DSN=Warehouse;UID=app;PWD=pw;")

    def test_driver_server_port_and_extra(self):
        cfg = {
            "host": "db1", "port": 1433, "database": "sales", "username": "u",
            "options": {"driver": "ODBC Driver 18 for SQL Server", "extra": "TrustServerCertificate=yes;"},
        }
        self.assertEqual(
            db_connect.odbc_connection_string(cfg, ""),
            "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db1,1433;DATABASE=sales;UID=u;TrustServerCertificate=yes;",
        )

    def test_special_characters_in_the_password_are_quoted(self):
        text = db_connect.odbc_connection_string({"options": {"dsn": "x"}}, "p;w}d")
        self.assertIn("PWD={p;w}}d};", text)


class SqliteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_existing_file_connects_and_reports_version(self):
        path = self.dir / "app.db"
        conn = sqlite3.connect(path)
        conn.execute("create table t (a)")
        conn.commit()
        conn.close()
        result = db_connect.test_connection({"kind": "sqlite", "database": str(path)})
        self.assertTrue(result["ok"], result)
        self.assertIn("Server:", result["message"])

    def test_missing_file_fails_with_a_clear_message(self):
        result = db_connect.test_connection({"kind": "sqlite", "database": str(self.dir / "nope.db")})
        self.assertFalse(result["ok"])
        self.assertIn("No file at", result["message"])

    def test_a_file_that_is_not_a_database_fails(self):
        path = self.dir / "junk.db"
        path.write_bytes(b"this is not sqlite" * 100)
        self.assertFalse(db_connect.test_connection({"kind": "sqlite", "database": str(path)})["ok"])


class ErrorHandlingTests(unittest.TestCase):
    def test_passwords_are_scrubbed_from_error_messages(self):
        text = db_connect._describe_error(RuntimeError("login failed for hunter2"), "hunter2")
        self.assertNotIn("hunter2", text)
        self.assertIn("***", text)

    @unittest.skipIf(importlib.util.find_spec("vertica_python"), "vertica_python is installed")
    def test_missing_driver_gives_an_install_hint(self):
        result = db_connect.test_connection({"kind": "vertica", "host": "h", "database": "d", "username": "u"})
        self.assertFalse(result["ok"])
        self.assertIn("pip install vertica-python", result["message"])

    def test_a_hung_driver_is_cut_off(self):
        import time

        original = db_connect.CONNECTORS["sqlite"]
        db_connect.CONNECTORS["sqlite"] = lambda cfg, pw, timeout: time.sleep(3)
        try:
            result = db_connect.test_connection({"kind": "sqlite", "database": "/x"}, timeout=-4)  # hard limit = 1 s
        finally:
            db_connect.CONNECTORS["sqlite"] = original
        self.assertFalse(result["ok"])
        self.assertIn("No answer", result["message"])


class SqlServerTests(unittest.TestCase):
    def run_with_fake_driver(self, cfg):
        seen = {}
        original_connector, original_available = db_connect.CONNECTORS["sqlserver"], db_connect.driver_available
        db_connect.driver_available = lambda spec: True

        def fake(c, password, timeout):
            seen.update(c)
            raise OSError("stop here")

        db_connect.CONNECTORS["sqlserver"] = fake
        try:
            result = db_connect.test_connection({"kind": "sqlserver", "username": "u", **cfg})
        finally:
            db_connect.CONNECTORS["sqlserver"] = original_connector
            db_connect.driver_available = original_available
        return seen, result

    def test_plain_host_gets_the_default_port(self):
        seen, _ = self.run_with_fake_driver({"host": "cobaka"})
        self.assertEqual(seen["port"], 1433)

    def test_named_instance_without_a_port_is_left_to_sql_browser(self):
        seen, _ = self.run_with_fake_driver({"host": "cobaka\\SQLEXPRESS"})
        self.assertIsNone(seen["port"])

    def test_named_instance_with_an_explicit_port_keeps_it(self):
        seen, _ = self.run_with_fake_driver({"host": "cobaka\\SQLEXPRESS", "port": 1444})
        self.assertEqual(seen["port"], 1444)

    def test_unreachable_server_error_explains_what_to_check(self):
        text = db_connect._describe_error(
            RuntimeError("DB-Lib error message 20009, Unable to connect: TDS server is unavailable (cobaka)"), "", "sqlserver"
        )
        self.assertIn("SQL Server Configuration Manager", text)
        self.assertIn("leave the port blank", text)

    def test_login_failure_error_mentions_authentication(self):
        text = db_connect._describe_error(RuntimeError("Login failed for user 'x' (18456)"), "", "sqlserver")
        self.assertIn("Trusted_Connection=yes", text)

    def test_other_databases_get_no_sql_server_hint(self):
        text = db_connect._describe_error(RuntimeError("20009"), "", "postgres")
        self.assertNotIn("Configuration Manager", text)
