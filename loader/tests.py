import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import engine_control
from . import worker as worker_module
from .engine import ERROR_SIDECAR, Engine, EngineConfig, Lane
from .worker import Worker, queue_counts, requeue_expired
from .models import ChecksumClaim, DatabaseConnection, DirectoryProfile, EngineSetting, LoadJob, Mapping, MappingRule, Record


class EngineTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        self.lane = Lane(None, "main", root / "stage", root / "processing", root / "archive", root / "fail")
        self.cfg = EngineConfig(
            heartbeat=root / "heartbeat",
            poll_interval=0.1,
            settle_seconds=0,
            probe_seconds=0,
            batch_size=2,  # small, so a bad row lands after earlier batches were inserted
        )
        self.engine = Engine(self.cfg, lanes=[self.lane])
        self.engine.prepare()

    def drop(self, name, text, lane=None):
        ((lane or self.lane).stage / name).write_text(text)

    def names(self, directory):
        return sorted(p.name for p in directory.iterdir())

    def test_valid_csv_is_loaded_and_archived(self):
        self.drop("ok.csv", "a,b\n1,2\n3,4\n5,6\n")
        self.assertEqual(self.engine.run_once(), 1)
        job = LoadJob.objects.get()
        self.assertEqual((job.status, job.rows_loaded), (LoadJob.Status.LOADED, 3))
        self.assertEqual(Record.objects.filter(job=job).count(), 3)
        self.assertEqual(self.names(self.lane.stage), [])
        self.assertEqual(len(self.names(self.lane.archive)), 1)
        self.assertTrue(job.final_path.endswith("ok.csv"))

    def test_bad_row_rolls_back_everything_and_moves_to_fail(self):
        self.drop("bad.csv", "a,b\n1,2\n3,4\n5\n")
        self.engine.run_once()
        job = LoadJob.objects.get()
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("row 3", job.error)
        self.assertEqual(Record.objects.count(), 0)
        fail_files = self.names(self.lane.fail)
        self.assertEqual(len(fail_files), 2)
        self.assertTrue(any(n.endswith(ERROR_SIDECAR) for n in fail_files))

    def test_unsupported_type_goes_to_fail(self):
        self.drop("notes.xyz", "hello")
        self.engine.run_once()
        self.assertEqual(LoadJob.objects.get().status, LoadJob.Status.FAILED)
        self.assertEqual(self.names(self.lane.archive), [])

    def test_empty_file_goes_to_fail(self):
        self.drop("empty.csv", "a,b\n")
        self.engine.run_once()
        self.assertEqual(LoadJob.objects.get().status, LoadJob.Status.FAILED)

    def test_duplicate_contents_are_rejected(self):
        self.drop("one.csv", "a\n1\n")
        self.engine.run_once()
        self.drop("two.csv", "a\n1\n")
        self.engine.run_once()
        statuses = list(LoadJob.objects.order_by("id").values_list("status", flat=True))
        self.assertEqual(statuses, [LoadJob.Status.LOADED, LoadJob.Status.FAILED])
        self.assertEqual(Record.objects.count(), 1)

    def test_files_still_settling_are_skipped(self):
        engine = Engine(dataclasses.replace(self.cfg, settle_seconds=3600), lanes=[self.lane])
        self.drop("new.csv", "a\n1\n")
        self.assertEqual(engine.run_once(), 0)
        self.assertEqual(self.names(self.lane.stage), ["new.csv"])

    def test_hidden_and_partial_files_are_ignored(self):
        self.drop(".hidden.csv", "a\n1\n")
        self.drop("big.csv.part", "a\n1\n")
        self.assertEqual(self.engine.run_once(), 0)

    def test_leftover_processing_files_are_requeued_on_start(self):
        (self.lane.processing / "stuck.csv").write_text("a\n1\n")
        restarted = Engine(self.cfg, lanes=[self.lane])  # a new process, as after a crash
        restarted.prepare()
        self.assertEqual(self.names(self.lane.stage), ["stuck.csv"])
        restarted.run_once()
        self.assertEqual(LoadJob.objects.get().status, LoadJob.Status.LOADED)

    def test_file_that_grows_during_probe_is_skipped_then_loaded(self):
        drop = self.drop
        lane = self.lane

        class GrowingEngine(Engine):
            grew = False

            def _pause(self, seconds):  # simulate a writer appending mid-probe
                if not self.grew:
                    self.grew = True
                    with open(lane.stage / "slow.csv", "a") as fh:
                        fh.write("3\n")

        engine = GrowingEngine(dataclasses.replace(self.cfg, probe_seconds=1), lanes=[lane])
        drop("slow.csv", "a\n1\n2\n")
        self.assertEqual(engine.run_once(), 0)  # changed during the probe
        self.assertEqual(LoadJob.objects.count(), 0)
        self.assertEqual(engine.run_once(), 1)  # stable on the next scan
        self.assertEqual(LoadJob.objects.get().rows_loaded, 3)

    def other_lane(self, name="second"):
        lane = Lane(None, name, self.root / name / "stage", self.root / name / "processing",
                    self.root / name / "archive", self.root / name / "fail")
        return lane

    def test_every_profile_is_served_and_files_stay_in_their_own_folders(self):
        second = self.other_lane()
        engine = Engine(self.cfg, lanes=[self.lane, second])
        engine.prepare()
        self.drop("a.csv", "a\n1\n")
        self.drop("b.csv", "a\n2\n", lane=second)
        self.drop("c.csv", "a,b\n3\n", lane=second)  # fails: ragged row
        engine.run_once()
        self.assertEqual(len(self.names(self.lane.archive)), 1)
        self.assertEqual(len(self.names(second.archive)), 1)
        self.assertEqual(len(self.names(second.fail)), 2)
        self.assertEqual(self.names(self.lane.fail), [])
        by_file = {j.filename: j.profile_name for j in LoadJob.objects.all()}
        self.assertEqual(by_file, {"a.csv": "main", "b.csv": "second", "c.csv": "second"})

    def test_a_profile_with_unusable_folders_does_not_stop_the_others(self):
        blocker = self.root / "blocker"
        blocker.write_text("i am a file, so folders cannot be created beneath me")
        broken = Lane(None, "broken", blocker / "stage", blocker / "p", blocker / "a", blocker / "f")
        engine = Engine(self.cfg, lanes=[broken, self.lane])
        engine.prepare()
        self.drop("ok.csv", "a\n1\n")
        self.assertEqual(engine.run_once(), 1)
        self.assertEqual([l.name for l in engine.lanes], ["main"])

    def test_leftover_processing_files_are_requeued_per_profile(self):
        second = self.other_lane()
        second.processing.mkdir(parents=True)
        (second.processing / "stuck.csv").write_text("a\n1\n")
        engine = Engine(self.cfg, lanes=[second])
        engine.prepare()
        self.assertEqual(self.names(second.stage), ["stuck.csv"])


class ConfigTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        context = override_settings(
            DATALOADER={
                "HEARTBEAT_FILE": self.root / "heartbeat",
                "POLL_INTERVAL": 5.0,
                "SETTLE_SECONDS": 2.0,
                "PROBE_SECONDS": 2.0,
                "BATCH_SIZE": 1000,
                "REJECT_DUPLICATES": True,
                "WORKERS": 0,
                "MAX_ATTEMPTS": 3,
            }
        )
        context.enable()
        self.addCleanup(context.disable)

    def put(self, payload):
        return self.client.put("/api/config/", data=json.dumps(payload), content_type="application/json")

    def test_get_lists_fields_with_defaults(self):
        body = self.client.get("/api/config/").json()
        keys = {f["key"]: f for f in body["fields"]}
        self.assertEqual(keys["POLL_INTERVAL"]["value"], 5.0)
        self.assertFalse(keys["POLL_INTERVAL"]["custom"])

    def test_saved_values_reach_the_engine_config(self):
        response = self.put({"POLL_INTERVAL": "1.5", "REJECT_DUPLICATES": False})
        self.assertEqual(response.status_code, 200)
        cfg = EngineConfig.from_settings()
        self.assertEqual(cfg.poll_interval, 1.5)
        self.assertFalse(cfg.reject_duplicates)

    def test_saving_the_default_removes_the_override(self):
        self.put({"POLL_INTERVAL": 1.5})
        self.assertEqual(EngineSetting.objects.count(), 1)
        self.put({"POLL_INTERVAL": 5})
        self.assertEqual(EngineSetting.objects.count(), 0)

    def test_invalid_values_are_rejected_with_field_errors(self):
        for payload in ({"BATCH_SIZE": 0}, {"POLL_INTERVAL": "abc"}, {"BATCH_SIZE": 1.5}, {"REJECT_DUPLICATES": "yes"}, {"NOPE": 1}):
            response = self.put(payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(next(iter(payload)), response.json()["errors"])
        self.assertEqual(EngineSetting.objects.count(), 0)

    def test_folders_are_no_longer_settings(self):
        response = self.put({"FAIL_DIR": str(self.root / "fail")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("FAIL_DIR", response.json()["errors"])

    def test_reset_restores_defaults(self):
        self.put({"BATCH_SIZE": 50})
        self.client.post("/api/config/reset/")
        self.assertEqual(EngineConfig.from_settings().batch_size, 1000)

    def test_running_engine_applies_setting_changes_on_its_next_scan(self):
        DirectoryProfile.objects.all().delete()
        engine = Engine()
        engine.prepare()
        self.put({"POLL_INTERVAL": 1.5})
        engine.run_once()
        self.assertEqual(engine.cfg.poll_interval, 1.5)


class EngineControlTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cfg = EngineConfig(
            heartbeat=root / "engine.heartbeat",
            settle_seconds=0,
            probe_seconds=0,
        )
        context = override_settings(
            DATALOADER={
                "HEARTBEAT_FILE": self.cfg.heartbeat,
                "POLL_INTERVAL": 5.0,
                "SETTLE_SECONDS": 2.0,
                "PROBE_SECONDS": 2.0,
                "BATCH_SIZE": 1000,
                "REJECT_DUPLICATES": True,
                "WORKERS": 0,
                "MAX_ATTEMPTS": 3,
            }
        )
        context.enable()
        self.addCleanup(context.disable)

    def beat(self, pid=None, age=0):
        self.cfg.heartbeat.write_text(
            json.dumps({"pid": pid or os.getpid(), "started_at": "2026-01-01T00:00:00+00:00", "time": "x"})
        )
        if age:
            old = time.time() - age
            os.utime(self.cfg.heartbeat, (old, old))

    def test_no_heartbeat_means_stopped(self):
        state = engine_control.read_state(self.cfg)
        self.assertFalse(state["running"])
        self.assertIsNone(state["pid"])

    def test_fresh_heartbeat_reports_running_and_pid(self):
        self.beat()
        state = engine_control.read_state(self.cfg)
        self.assertTrue(state["running"])
        self.assertEqual(state["pid"], os.getpid())
        self.assertFalse(state["stopping"])

    def test_stale_or_garbage_heartbeat_means_stopped(self):
        self.beat(age=60)
        self.assertFalse(engine_control.read_state(self.cfg)["running"])
        self.cfg.heartbeat.write_text("not json")
        self.assertFalse(engine_control.read_state(self.cfg)["running"])

    def test_stop_request_writes_stop_file_and_reports_stopping(self):
        self.beat()
        state = engine_control.request_stop(self.cfg)
        self.assertTrue(self.cfg.stop_file.exists())
        self.assertTrue(state["stopping"])

    def test_stop_when_not_running_is_refused(self):
        with self.assertRaises(engine_control.EngineError):
            engine_control.request_stop(self.cfg)
        self.assertFalse(self.cfg.stop_file.exists())

    def test_engine_writes_a_pid_heartbeat_and_honours_the_stop_file(self):
        engine = Engine(self.cfg, lanes=[])
        engine.prepare()
        engine._write_heartbeat()
        self.assertEqual(json.loads(self.cfg.heartbeat.read_text())["pid"], os.getpid())
        engine._check_stop_file()
        self.assertFalse(engine._stop.is_set())
        self.cfg.stop_file.write_text("x")
        engine._check_stop_file()
        self.assertTrue(engine._stop.is_set())

    def test_api_reports_engine_state_and_validates_stop(self):
        body = self.client.get("/api/stats/").json()
        self.assertFalse(body["engine_online"])
        self.assertIsNone(body["engine"]["pid"])
        self.assertEqual(self.client.post("/api/engine/stop/").status_code, 409)
        self.beat()
        body = self.client.get("/api/stats/").json()
        self.assertTrue(body["engine_online"])
        self.assertEqual(body["engine"]["pid"], os.getpid())
        self.assertEqual(self.client.post("/api/engine/stop/").status_code, 200)
        self.assertTrue(self.cfg.stop_file.exists())

    def test_start_is_refused_when_already_running(self):
        self.beat()
        self.assertEqual(self.client.post("/api/engine/start/").status_code, 409)


class DatabaseApiTests(TestCase):
    def send(self, method, path, payload=None):
        return getattr(self.client, method)(path, data=json.dumps(payload or {}), content_type="application/json")

    def sqlite_payload(self, name="Local", path=None, **extra):
        return {"name": name, "kind": "sqlite", "database": path or str(Path(tempfile.gettempdir()) / "x.db"), **extra}

    def test_lists_kinds_in_the_requested_order(self):
        body = self.client.get("/api/databases/").json()
        self.assertEqual(
            [k["label"] for k in body["kinds"]],
            ["Oracle", "SQL Server", "Vertica", "PostgreSQL", "MySQL", "SQLite", "ODBC"],
        )

    def test_create_update_and_delete(self):
        created = self.send("post", "/api/databases/", self.sqlite_payload())
        self.assertEqual(created.status_code, 201)
        pk = created.json()["id"]
        updated = self.send("put", f"/api/databases/{pk}/", self.sqlite_payload(name="Renamed"))
        self.assertEqual(updated.json()["name"], "Renamed")
        self.assertEqual(len(self.client.get("/api/databases/").json()["connections"]), 1)
        self.assertEqual(self.send("delete", f"/api/databases/{pk}/").status_code, 200)
        self.assertEqual(self.client.get("/api/databases/").json()["connections"], [])

    def test_names_are_unique_ignoring_case(self):
        self.send("post", "/api/databases/", self.sqlite_payload(name="Sales"))
        response = self.send("post", "/api/databases/", self.sqlite_payload(name="sales"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json()["errors"])

    def test_validation_errors_name_the_field(self):
        cases = [
            ({"name": "A", "kind": "postgres", "database": "d", "username": "u"}, "host"),
            ({"name": "A", "kind": "postgres", "host": "h", "database": "d", "username": "u", "port": "99999"}, "port"),
            ({"name": "A", "kind": "sqlite", "database": "relative.db"}, "database"),
            ({"name": "A", "kind": "odbc"}, "dsn"),
            ({"name": "", "kind": "mysql", "host": "h", "username": "u"}, "name"),
            ({"name": "A", "kind": "nope"}, "kind"),
        ]
        for payload, field in cases:
            response = self.send("post", "/api/databases/", payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.json()["errors"], payload)

    def test_password_is_encrypted_never_returned_and_kept_when_blank(self):
        from .models import DatabaseConnection

        payload = {"name": "PG", "kind": "postgres", "host": "h", "database": "d", "username": "u", "password": "s3cret"}
        created = self.send("post", "/api/databases/", payload)
        body = created.json()
        self.assertTrue(body["has_password"])
        self.assertNotIn("s3cret", json.dumps(body))
        stored = DatabaseConnection.objects.get(pk=body["id"]).password_enc
        self.assertTrue(stored and "s3cret" not in stored)
        self.send("put", f"/api/databases/{body['id']}/", {**payload, "password": "", "host": "h2"})
        self.assertEqual(DatabaseConnection.objects.get(pk=body["id"]).password_enc, stored)
        self.send("put", f"/api/databases/{body['id']}/", {**payload, "password": "", "clear_password": True})
        self.assertEqual(DatabaseConnection.objects.get(pk=body["id"]).password_enc, "")

    def test_test_endpoint_succeeds_for_a_real_sqlite_file(self):
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "ok.db"
        conn = sqlite3.connect(path)
        conn.execute("create table t (a)")
        conn.commit()
        conn.close()
        result = self.send("post", "/api/databases/test/", {"kind": "sqlite", "database": str(path)}).json()
        self.assertTrue(result["ok"], result)

    def test_test_endpoint_reports_failure_and_field_errors(self):
        missing = str(Path(tempfile.gettempdir()) / "definitely-not-here.db")
        result = self.send("post", "/api/databases/test/", {"kind": "sqlite", "database": missing}).json()
        self.assertFalse(result["ok"])
        self.assertEqual(self.send("post", "/api/databases/test/", {"kind": "postgres"}).status_code, 400)



class ProfileApiTests(TestCase):
    def setUp(self):
        DirectoryProfile.objects.all().delete()  # the migration's Default profile points at C:\\DataLoader
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        context = override_settings(
            DATALOADER={
                "HEARTBEAT_FILE": self.root / "heartbeat",
                "POLL_INTERVAL": 5.0, "SETTLE_SECONDS": 0.0, "PROBE_SECONDS": 0.0,
                "BATCH_SIZE": 1000, "REJECT_DUPLICATES": True, "WORKERS": 0, "MAX_ATTEMPTS": 3,
            }
        )
        context.enable()
        self.addCleanup(context.disable)

    def send(self, method, path, payload=None):
        return getattr(self.client, method)(path, data=json.dumps(payload or {}), content_type="application/json")

    def folders(self, name):
        base = self.root / name
        return {
            "stage_dir": str(base / "stage"), "processing_dir": str(base / "processing"),
            "archive_dir": str(base / "archive"), "fail_dir": str(base / "fail"),
        }

    def make(self, name="Sales", **extra):
        response = self.send("post", "/api/directories/", {"name": name, **self.folders(name), **extra})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_create_list_update_delete(self):
        created = self.make("Sales")
        self.assertTrue(created["enabled"])
        self.assertEqual(created["counts"], {"stage": 0, "processing": 0, "archive": 0, "fail": 0})
        self.make("Finance")
        listed = self.client.get("/api/directories/").json()["profiles"]
        self.assertEqual([p["name"] for p in listed], ["Finance", "Sales"])
        updated = self.send("put", f"/api/directories/{created['id']}/", {"enabled": False, "name": "Sales EU"}).json()
        self.assertEqual((updated["name"], updated["enabled"]), ("Sales EU", False))
        self.assertEqual(self.send("delete", f"/api/directories/{created['id']}/").status_code, 200)
        self.assertEqual(len(self.client.get("/api/directories/").json()["profiles"]), 1)

    def test_max_parallel_defaults_to_unlimited_and_is_validated(self):
        self.assertEqual(self.make("A")["max_parallel"], 0)
        limited = self.make("B", max_parallel=2)
        self.assertEqual(limited["max_parallel"], 2)
        self.assertEqual(self.send("put", f"/api/directories/{limited['id']}/", {"max_parallel": "1"}).json()["max_parallel"], 1)
        for bad in (-1, 65, 1.5, "x", True):
            response = self.send("put", f"/api/directories/{limited['id']}/", {"max_parallel": bad})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("max_parallel", response.json()["errors"])

    def test_stats_report_the_queue(self):
        LoadJob.objects.create(status=LoadJob.Status.QUEUED, filename="a.csv")
        LoadJob.objects.create(status=LoadJob.Status.PROCESSING, filename="b.csv", worker="worker-1-9", attempts=1)
        body = self.client.get("/api/stats/").json()
        self.assertEqual(body["queue"], {"queued": 1, "processing": 1})
        jobs = self.client.get("/api/jobs/").json()["jobs"]
        self.assertIn("attempts", jobs[0])

    def test_validation_errors_name_the_field(self):
        base = self.folders("X")
        cases = [
            ({"name": "", **base}, "name"),
            ({"name": "X", **base, "stage_dir": "relative/stage"}, "stage_dir"),
            ({"name": "X", **base, "fail_dir": base["archive_dir"]}, "fail_dir"),
            ({"name": "X", **base, "enabled": "yes"}, "enabled"),
            ({"name": "X", **base, "bogus": 1}, "bogus"),
        ]
        for payload, field in cases:
            response = self.send("post", "/api/directories/", payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.json()["errors"], payload)
        self.assertEqual(DirectoryProfile.objects.count(), 0)

    def test_names_are_unique_ignoring_case(self):
        self.make("Sales")
        response = self.send("post", "/api/directories/", {"name": "sales", **self.folders("other")})
        self.assertIn("name", response.json()["errors"])

    def test_profiles_cannot_share_stage_or_processing_but_may_share_archive_and_fail(self):
        self.make("A")
        mine = self.folders("B")
        clash = self.send("post", "/api/directories/", {"name": "B", **mine, "stage_dir": self.folders("A")["stage_dir"]})
        self.assertIn("stage_dir", clash.json()["errors"])
        shared = self.send("post", "/api/directories/", {
            "name": "B", **mine,
            "archive_dir": self.folders("A")["archive_dir"], "fail_dir": self.folders("A")["fail_dir"],
        })
        self.assertEqual(shared.status_code, 201, shared.content)
        # One profile's archive may not be another's stage.
        bad = self.send("post", "/api/directories/", {
            "name": "C", **self.folders("C"), "archive_dir": self.folders("A")["stage_dir"]})
        self.assertIn("archive_dir", bad.json()["errors"])

    def test_editing_a_profile_does_not_conflict_with_itself(self):
        created = self.make("A")
        response = self.send("put", f"/api/directories/{created['id']}/", {**self.folders("A"), "name": "A"})
        self.assertEqual(response.status_code, 200, response.content)

    def test_folder_test_reports_each_folder_without_saving(self):
        (self.root / "exists").mkdir()
        body = {**self.folders("T"), "stage_dir": str(self.root / "exists")}
        result = self.send("post", "/api/directories/test/", body).json()
        self.assertTrue(result["ok"])
        self.assertIn("exists", result["results"]["stage_dir"]["message"])
        self.assertIn("will be created", result["results"]["fail_dir"]["message"])
        bad = self.send("post", "/api/directories/test/", {**body, "fail_dir": "nope"}).json()
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["results"]["fail_dir"]["ok"])
        self.assertEqual(DirectoryProfile.objects.count(), 0)
        self.assertFalse((self.root / "T").exists())  # testing creates nothing

    def test_upload_lands_in_that_profiles_stage_and_lists_there(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        a, b = self.make("A"), self.make("B")
        file = SimpleUploadedFile("rows.csv", b"a\n1\n")
        response = self.client.post(f"/api/directories/{a['id']}/upload/", {"file": file})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([f["name"] for f in self.client.get(f"/api/directories/{a['id']}/files/stage/").json()["files"]], ["rows.csv"])
        self.assertEqual(self.client.get(f"/api/directories/{b['id']}/files/stage/").json()["files"], [])
        bad = self.client.post(f"/api/directories/{a['id']}/upload/", {"file": SimpleUploadedFile("x.exe", b"1")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get(f"/api/directories/{a['id']}/files/nope/").status_code, 404)

    def test_engine_loads_every_enabled_profile_and_skips_disabled_ones(self):
        a, b, off = self.make("A"), self.make("B"), self.make("Off", enabled=False)
        for p in (a, b, off):
            Path(p["stage_dir"]).mkdir(parents=True, exist_ok=True)
            (Path(p["stage_dir"]) / "f.csv").write_text(f"v\n{p['name']}\n")
        engine = Engine()
        engine.prepare()
        self.assertEqual(engine.run_once(), 2)
        self.assertEqual(sorted(LoadJob.objects.values_list("profile_name", flat=True)), ["A", "B"])
        self.assertTrue((Path(off["stage_dir"]) / "f.csv").exists())
        # Enable it while the engine is running: picked up on the next scan.
        self.send("put", f"/api/directories/{off['id']}/", {"enabled": True})
        self.assertEqual(engine.run_once(), 1)

    def test_stats_total_enabled_profiles_and_list_each(self):
        a, off = self.make("A"), self.make("Off", enabled=False)
        (Path(a["stage_dir"])).mkdir(parents=True, exist_ok=True)
        (Path(a["stage_dir"]) / "f.csv").write_text("v\n1\n")
        Path(off["stage_dir"]).mkdir(parents=True, exist_ok=True)
        (Path(off["stage_dir"]) / "g.csv").write_text("v\n1\n")
        body = self.client.get("/api/stats/").json()
        self.assertEqual(body["directories"]["stage"], 1)
        self.assertEqual({p["name"]: p["counts"]["stage"] for p in body["profiles"]}, {"A": 1, "Off": 1})

    def test_retry_moves_the_file_to_its_own_profiles_stage(self):
        a = self.make("A")
        Path(a["stage_dir"]).mkdir(parents=True, exist_ok=True)
        Path(a["stage_dir"], "bad.csv").write_text("a,b\n1\n")
        engine = Engine()
        engine.prepare()
        engine.run_once()
        job = LoadJob.objects.get()
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertEqual(self.client.post(f"/api/jobs/{job.pk}/retry/").status_code, 200)
        self.assertEqual(sorted(p.name for p in Path(a["stage_dir"]).iterdir()), ["bad.csv"])

    def test_retry_is_refused_when_the_profile_was_deleted(self):
        a = self.make("A")
        Path(a["stage_dir"]).mkdir(parents=True, exist_ok=True)
        Path(a["stage_dir"], "bad.csv").write_text("a,b\n1\n")
        engine = Engine()
        engine.prepare()
        engine.run_once()
        self.send("delete", f"/api/directories/{a['id']}/")
        job = LoadJob.objects.get()
        self.assertEqual(job.profile_name, "A")  # the name survives
        self.assertEqual(self.client.post(f"/api/jobs/{job.pk}/retry/").status_code, 409)

    def test_migration_created_a_default_profile(self):
        # setUp deleted it; the migration function itself is exercised through a fresh test database.
        from importlib import import_module

        migration = import_module("loader.migrations.0004_directoryprofile")
        from django.apps import apps

        migration.create_default_profile(apps, None)
        self.assertEqual(list(DirectoryProfile.objects.values_list("name", flat=True)), ["Default"])



class QueueTests(TestCase):
    """The scanner queues files; workers claim, lease, retry and load them."""

    def setUp(self):
        DirectoryProfile.objects.all().delete()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lane = Lane(None, "q", self.root / "stage", self.root / "processing", self.root / "archive", self.root / "fail")
        self.cfg = EngineConfig(heartbeat=self.root / "hb", poll_interval=0.1, settle_seconds=0, probe_seconds=0,
                                batch_size=2, workers=1, max_attempts=3)  # workers=1: the scanner only queues
        self.engine = Engine(self.cfg, lanes=[self.lane])
        self.engine.prepare()
        self.w1 = Worker(lambda: self.cfg, "w1")
        self.w2 = Worker(lambda: self.cfg, "w2")

    def names(self, folder):
        return sorted(p.name for p in folder.iterdir() if not p.name.endswith(ERROR_SIDECAR))

    def queue(self, name="a.csv", text="x\n1\n2\n"):
        (self.lane.stage / name).write_text(text)
        self.assertEqual(self.engine.run_once(), 1)
        return LoadJob.objects.order_by("-id").first()

    def test_scanner_queues_the_file_into_processing_without_loading_it(self):
        job = self.queue("a.csv")
        self.assertEqual(job.status, LoadJob.Status.QUEUED)
        self.assertEqual(self.names(self.lane.stage), [])
        self.assertTrue(Path(job.path).is_file() and Path(job.path).parent == self.lane.processing)
        self.assertEqual((job.filename, job.profile_name, job.archive_dir), ("a.csv", "q", str(self.lane.archive)))
        self.assertEqual(Record.objects.count(), 0)

    def test_same_named_files_do_not_collide_in_processing(self):
        self.queue("same.csv", "x\n1\n")
        self.queue("same.csv", "x\n2\n")
        self.assertEqual(len(self.names(self.lane.processing)), 2)

    def test_a_worker_loads_a_queued_job_and_archives_it(self):
        job = self.queue()
        self.assertEqual(self.w1.drain(), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.rows_loaded, job.attempts), (LoadJob.Status.LOADED, 2, 1))
        self.assertIsNone(job.lease_until)
        self.assertTrue(job.final_path.startswith(str(self.lane.archive)) and job.final_path.endswith("_a.csv"))
        self.assertEqual(self.names(self.lane.processing), [])
        self.assertEqual(ChecksumClaim.objects.count(), 0)

    def test_only_one_worker_can_claim_a_job(self):
        job = self.queue()
        first, second = self.w1.claim_next(), self.w2.claim_next()
        self.assertEqual(first.pk, job.pk)
        self.assertIsNone(second)
        job.refresh_from_db()
        self.assertEqual((job.status, job.worker, job.attempts), (LoadJob.Status.PROCESSING, "w1", 1))
        self.assertIsNotNone(job.lease_until)

    def test_jobs_are_claimed_oldest_first(self):
        a, b = self.queue("a.csv"), self.queue("b.csv", "x\n9\n")
        self.assertEqual([self.w1.claim_next().pk, self.w2.claim_next().pk], [a.pk, b.pk])

    def test_a_dead_workers_job_goes_back_to_the_queue_when_its_lease_expires(self):
        job = self.queue()
        self.w1.claim_next()
        self.assertEqual(requeue_expired(3), 0)  # lease still valid
        LoadJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timezone.timedelta(seconds=1))
        self.assertEqual(requeue_expired(3), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.QUEUED)
        self.assertEqual(self.w2.drain(), 1)  # another worker finishes it
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (LoadJob.Status.LOADED, 2))

    def test_a_job_that_keeps_killing_workers_is_failed_after_max_attempts(self):
        job = self.queue()
        LoadJob.objects.filter(pk=job.pk).update(attempts=2)
        self.w1.claim_next()  # attempt 3
        LoadJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timezone.timedelta(seconds=1))
        requeue_expired(3)
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("Gave up", job.error)
        self.assertEqual(len(self.names(self.lane.fail)), 1)

    def test_a_temporary_error_is_retried_with_a_delay_then_succeeds(self):
        from django.db import OperationalError

        job = self.queue()
        calls = {"n": 0}
        real = worker_module.load_records

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("connection lost")
            return real(*args, **kwargs)

        with mock.patch.object(worker_module, "load_records", flaky):
            self.w1.drain()
            job.refresh_from_db()
            self.assertEqual(job.status, LoadJob.Status.QUEUED)
            self.assertIn("will retry", job.error)
            self.assertGreater(job.next_attempt_at, timezone.now())
            self.assertEqual(self.w1.drain(), 0)  # not claimable until the delay has passed
            LoadJob.objects.filter(pk=job.pk).update(next_attempt_at=None)
            self.w1.drain()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.error), (LoadJob.Status.LOADED, 2, ""))
        self.assertEqual(Record.objects.filter(job=job).count(), 2)  # no half-load from the first attempt

    def test_a_temporary_error_on_the_last_attempt_fails_the_file(self):
        from django.db import OperationalError

        job = self.queue()
        LoadJob.objects.filter(pk=job.pk).update(attempts=2)
        with mock.patch.object(worker_module, "load_records", side_effect=OperationalError("still down")):
            self.w1.drain()
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("OperationalError", job.error)
        self.assertEqual(len(self.names(self.lane.fail)), 1)

    def test_a_bad_file_is_not_retried(self):
        job = self.queue("bad.csv", "a,b\n1\n")
        self.w1.drain()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (LoadJob.Status.FAILED, 1))

    def test_identical_files_never_load_at_the_same_time_or_twice(self):
        first, second = self.queue("one.csv", "x\n1\n"), self.queue("two.csv", "x\n1\n")
        digest = worker_module.sha256_of(Path(first.path))
        ChecksumClaim.objects.create(checksum=digest, job=first)  # as if one.csv were loading on another worker
        LoadJob.objects.filter(pk=first.pk).update(status=LoadJob.Status.PROCESSING, worker="other", lease_until=timezone.now() + timezone.timedelta(minutes=1))
        self.w1.drain()
        second.refresh_from_db()
        self.assertEqual((second.status, second.attempts), (LoadJob.Status.QUEUED, 0))  # waits, no attempt used
        ChecksumClaim.objects.all().delete()
        LoadJob.objects.filter(pk=first.pk).update(status=LoadJob.Status.LOADED, checksum=digest)
        LoadJob.objects.filter(pk=second.pk).update(next_attempt_at=None)
        self.w1.drain()
        second.refresh_from_db()
        self.assertEqual(second.status, LoadJob.Status.FAILED)
        self.assertIn("already loaded", second.error)

    def test_a_worker_that_lost_its_lease_rolls_back_and_does_not_finish(self):
        job = self.queue()
        real = worker_module.load_records

        def steal(j, rows, size):
            count = real(j, rows, size)
            LoadJob.objects.filter(pk=j.pk).update(worker="someone-else")  # the lease was given away mid-load
            return count

        with mock.patch.object(worker_module, "load_records", steal):
            self.w1.drain()
        self.assertEqual(Record.objects.count(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.PROCESSING)
        self.assertEqual(self.names(self.lane.archive), [])

    def test_max_parallel_one_loads_a_profiles_files_one_at_a_time_in_order(self):
        profile = DirectoryProfile.objects.create(name="Ordered", stage_dir="s", processing_dir="p", archive_dir="a", fail_dir="f", max_parallel=1)
        a, b = self.queue("a.csv"), self.queue("b.csv", "x\n9\n")
        LoadJob.objects.filter(pk__in=[a.pk, b.pk]).update(profile=profile)
        first = self.w1.claim_next()
        self.assertEqual(first.pk, a.pk)
        self.assertIsNone(self.w2.claim_next())  # b has to wait for a
        b.refresh_from_db()
        self.assertEqual((b.status, b.attempts), (LoadJob.Status.QUEUED, 0))
        LoadJob.objects.filter(pk=b.pk).update(next_attempt_at=None)
        self.w1.process(first)
        self.assertEqual(self.w2.claim_next().pk, b.pk)

    def test_max_parallel_two_allows_two_at_once(self):
        profile = DirectoryProfile.objects.create(name="Two", stage_dir="s", processing_dir="p", archive_dir="a", fail_dir="f", max_parallel=2)
        jobs = [self.queue(f"{n}.csv", f"x\n{n}\n") for n in range(3)]
        LoadJob.objects.filter(pk__in=[j.pk for j in jobs]).update(profile=profile)
        self.assertIsNotNone(self.w1.claim_next())
        self.assertIsNotNone(self.w2.claim_next())
        self.assertIsNone(Worker(lambda: self.cfg, "w3").claim_next())

    def test_engine_start_queues_jobs_a_previous_run_left_loading(self):
        job = self.queue()
        self.w1.claim_next()
        Engine(self.cfg, lanes=[self.lane]).prepare()  # a fresh engine process
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.QUEUED)
        self.assertEqual(self.names(self.lane.processing).__len__(), 1)  # the file stayed where it was

    def test_orphan_files_return_to_stage_but_queued_files_stay(self):
        job = self.queue()
        (self.lane.processing / "orphan.csv").write_text("x\n1\n")
        Engine(self.cfg, lanes=[self.lane]).prepare()
        self.assertEqual(self.names(self.lane.stage), ["orphan.csv"])
        self.assertTrue(Path(job.path).exists())

    def test_a_missing_file_fails_its_job(self):
        job = self.queue()
        Path(job.path).unlink()
        self.w1.drain()
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.FAILED)

    def test_queue_counts(self):
        self.queue("a.csv"), self.queue("b.csv", "x\n2\n")
        self.w1.claim_next()
        self.assertEqual(queue_counts(), {"queued": 1, "processing": 1})

    def test_inline_mode_loads_what_it_queues(self):
        inline = Engine(dataclasses.replace(self.cfg, workers=0), lanes=[self.lane])
        inline.prepare()
        (self.lane.stage / "a.csv").write_text("x\n1\n")
        self.assertEqual(inline.run_once(), 1)
        self.assertEqual(LoadJob.objects.get().status, LoadJob.Status.LOADED)


class WorkerSupervisionTests(TestCase):
    """The engine keeps the configured number of worker processes running."""

    CODE = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.05)\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cfg = EngineConfig(heartbeat=root / "hb", poll_interval=0.1, workers=2)
        self.engine = Engine(self.cfg, lanes=[])
        self.engine.cfg.workers_dir.mkdir(parents=True)
        self.engine._worker_command = lambda slot: [
            sys.executable, "-c", self.CODE, str(self.cfg.workers_dir / f"worker-{slot}.stop")
        ]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.engine._workers.values():
            process.kill()
            process.wait()

    def test_starts_the_configured_number_and_restarts_one_that_dies(self):
        self.engine._supervise_workers()
        self.assertEqual(sorted(self.engine._workers), [1, 2])
        victim = self.engine._workers[1]
        victim.kill()
        victim.wait()
        self.engine._supervise_workers()
        self.assertEqual(sorted(self.engine._workers), [1, 2])
        self.assertNotEqual(self.engine._workers[1].pid, victim.pid)
        self.assertEqual([w["slot"] for w in self.engine._worker_report()], [1, 2])

    def test_a_dead_workers_job_is_queued_again_at_once(self):
        self.engine._supervise_workers()
        process = self.engine._workers[1]
        job = LoadJob.objects.create(status=LoadJob.Status.PROCESSING, filename="x.csv", worker=f"worker-1-{process.pid}",
                                     attempts=1, lease_until=timezone.now() + timezone.timedelta(minutes=1))
        process.kill()
        process.wait()
        self.engine._supervise_workers()
        job.refresh_from_db()
        self.assertEqual(job.status, LoadJob.Status.QUEUED)  # no waiting for the lease

    def test_scales_down_by_asking_extras_to_finish_and_leave(self):
        self.engine._supervise_workers()
        self.engine.cfg = dataclasses.replace(self.cfg, workers=1)
        self.engine._supervise_workers()
        self.engine._workers[2].wait(timeout=5)
        self.engine._supervise_workers()
        self.assertEqual(sorted(self.engine._workers), [1])

    def test_stop_waits_for_every_worker_to_exit(self):
        self.engine._supervise_workers()
        self.engine._stop_workers()
        self.assertEqual(self.engine._workers, {})

    def test_no_new_workers_start_once_stopping(self):
        self.engine.stop()
        self.engine._supervise_workers()
        self.assertEqual(self.engine._workers, {})


class MappingTests(TestCase):
    """Mappings: which connection a profile loads into and which file names go to which table."""

    def setUp(self):
        DirectoryProfile.objects.all().delete()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.profile = self.profile_named("Hr")
        self.oracle = DatabaseConnection.objects.create(name="ORACLE_CONNECT", kind="oracle", host="h", database="svc")
        self.sqlb = DatabaseConnection.objects.create(name="ODBC_SQLB", kind="odbc", options={"dsn": "x"})

    def profile_named(self, name):
        base = self.root / name
        return DirectoryProfile.objects.create(
            name=name, stage_dir=str(base / "stage"), processing_dir=str(base / "processing"),
            archive_dir=str(base / "archive"), fail_dir=str(base / "fail"),
        )

    def send(self, method, path, payload=None):
        return getattr(self.client, method)(path, data=json.dumps(payload or {}), content_type="application/json")

    RULES = [{"pattern": "emp*.*", "table": "dbo.EMP"}, {"pattern": "dep*.*", "table": "delta.DEP"}]

    def make(self, profile=None, connection=None, **extra):
        body = {"profile": (profile or self.profile).pk, "connection": (connection or self.oracle).pk, "rules": self.RULES, **extra}
        response = self.send("post", "/api/mappings/", body)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_create_list_update_delete(self):
        made = self.make()
        self.assertEqual((made["profile_name"], made["connection_name"], made["enabled"]), ("Hr", "ORACLE_CONNECT", True))
        self.assertEqual(made["rules"], [{**r, "create_table": False, "merge_keys": ""} for r in self.RULES])
        listed = self.client.get("/api/mappings/").json()["mappings"]
        self.assertEqual([m["id"] for m in listed], [made["id"]])
        changed = self.send("put", f"/api/mappings/{made['id']}/", {
            "connection": self.sqlb.pk, "enabled": False,
            "rules": [{"pattern": "job*.xml", "table": "JOB"}, *self.RULES],
        }).json()
        self.assertEqual((changed["connection_name"], changed["enabled"]), ("ODBC_SQLB", False))
        self.assertEqual([r["pattern"] for r in changed["rules"]], ["job*.xml", "emp*.*", "dep*.*"])  # order is kept
        self.assertEqual(self.send("put", f"/api/mappings/{made['id']}/", {"enabled": True}).json()["rules"][0]["pattern"], "job*.xml")
        self.assertEqual(self.send("delete", f"/api/mappings/{made['id']}/").status_code, 200)
        self.assertTrue(DirectoryProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertTrue(DatabaseConnection.objects.filter(pk=self.oracle.pk).exists())

    def test_one_mapping_per_profile_and_the_profile_cannot_change(self):
        made = self.make()
        again = self.send("post", "/api/mappings/", {"profile": self.profile.pk, "connection": self.sqlb.pk, "rules": self.RULES})
        self.assertEqual(again.status_code, 400)
        self.assertIn("already has a mapping", again.json()["errors"]["profile"])
        other = self.profile_named("Fin")
        moved = self.send("put", f"/api/mappings/{made['id']}/", {"profile": other.pk})
        self.assertIn("profile", moved.json()["errors"])

    def test_merge_keys_are_validated_and_saved(self):
        base = {"profile": self.profile.pk, "connection": self.oracle.pk}
        bad = self.send("post", "/api/mappings/", {**base, "rules": [{"pattern": "a*", "table": "T", "merge_keys": "id; drop"}]})
        self.assertIn("rules.0.merge_keys", bad.json()["errors"])
        ok = self.send("post", "/api/mappings/", {**base, "rules": [{"pattern": "a*", "table": "T", "merge_keys": " dept ,empno "}]})
        self.assertEqual(ok.json()["rules"][0]["merge_keys"], "dept, empno")

    def test_validation_names_the_field(self):
        base = {"profile": self.profile.pk, "connection": self.oracle.pk}
        cases = [
            ({**base, "profile": 999, "rules": self.RULES}, "profile"),
            ({**base, "connection": None, "rules": self.RULES}, "connection"),
            ({**base, "rules": []}, "rules"),
            ({**base, "rules": [{"pattern": "", "table": "T"}]}, "rules.0.pattern"),
            ({**base, "rules": [{"pattern": "a/b*", "table": "T"}]}, "rules.0.pattern"),
            ({**base, "rules": [{"pattern": "a*", "table": ""}]}, "rules.0.table"),
            ({**base, "rules": [{"pattern": "a*", "table": "dbo.EMP; DROP TABLE x"}]}, "rules.0.table"),
            ({**base, "rules": [{"pattern": "a*", "table": "a.b.c.d"}]}, "rules.0.table"),
            ({**base, "rules": [{"pattern": "a*", "table": "1abc"}]}, "rules.0.table"),
            ({**base, "rules": [{"pattern": "a*", "table": "T"}, {"pattern": "A*", "table": "U"}]}, "rules.1.pattern"),
            ({**base, "rules": self.RULES, "enabled": "yes"}, "enabled"),
            ({**base, "rules": self.RULES, "bogus": 1}, "bogus"),
        ]
        for payload, field in cases:
            response = self.send("post", "/api/mappings/", payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.json()["errors"], payload)
        self.assertEqual(Mapping.objects.count(), 0)

    def test_table_names_with_schema_and_database_are_accepted(self):
        rules = [{"pattern": "a*", "table": "EMP"}, {"pattern": "b*", "table": "dbo.EMP"}, {"pattern": "c*", "table": "Hr.dbo.EMP_2$"}]
        self.make(rules=rules)

    def test_patterns_ignore_case_and_use_the_file_name_only(self):
        self.make()
        names = ["emp001.json", "EMP002.JSON", "emp.json", "dep_3243.xml", "Dep7.TXT", "job.xml", "xemp1.json", "emp001", "employees.csv"]
        out = {r["filename"]: r["table"] for r in self.send("post", "/api/mappings/match/", {"rules": self.RULES, "filenames": names}).json()["results"]}
        self.assertEqual(out["emp001.json"], "dbo.EMP")
        self.assertEqual(out["EMP002.JSON"], "dbo.EMP")
        self.assertEqual(out["emp.json"], "dbo.EMP")  # * may match nothing
        self.assertEqual(out["dep_3243.xml"], "delta.DEP")
        self.assertEqual(out["Dep7.TXT"], "delta.DEP")
        self.assertIsNone(out["job.xml"])
        self.assertIsNone(out["xemp1.json"])  # must start with emp
        self.assertIsNone(out["emp001"])  # emp*.* needs a dot
        self.assertEqual(out["employees.csv"], "dbo.EMP")  # emp* then .* : "employees" + ".csv"

    def test_first_matching_rule_wins(self):
        rules = [{"pattern": "emp_old*.*", "table": "dbo.EMP_OLD"}, {"pattern": "emp*.*", "table": "dbo.EMP"}]
        results = self.send("post", "/api/mappings/match/", {"rules": rules, "filenames": ["emp_old1.csv", "emp2.csv"]}).json()["results"]
        self.assertEqual([(r["rule"], r["table"]) for r in results], [(0, "dbo.EMP_OLD"), (1, "dbo.EMP")])

    def test_match_needs_lists(self):
        self.assertEqual(self.send("post", "/api/mappings/match/", {"rules": "x", "filenames": []}).status_code, 400)

    def test_resolve_target_for_the_loader(self):
        from loader import mapping as mapping_module

        made = self.make()
        connection, table = mapping_module.resolve_target(self.profile, "emp001.json")
        self.assertEqual((connection.pk, table), (self.oracle.pk, "dbo.EMP"))
        self.assertIsNone(mapping_module.resolve_target(self.profile, "job.xml"))
        self.send("put", f"/api/mappings/{made['id']}/", {"enabled": False})
        self.assertIsNone(mapping_module.resolve_target(self.profile, "emp001.json"))  # switched off
        self.assertIsNone(mapping_module.resolve_target(self.profile_named("Other"), "emp001.json"))  # no mapping

    def test_a_connection_in_use_cannot_be_deleted(self):
        made = self.make()
        response = self.client.delete(f"/api/databases/{self.oracle.pk}/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Hr", response.json()["error"])
        self.send("delete", f"/api/mappings/{made['id']}/")
        self.assertEqual(self.client.delete(f"/api/databases/{self.oracle.pk}/").status_code, 200)

    def test_deleting_the_profile_removes_its_mapping(self):
        self.make()
        self.profile.delete()
        self.assertEqual((Mapping.objects.count(), MappingRule.objects.count()), (0, 0))


class MappingTableTests(TestCase):
    """Browsing and verifying the tables of a mapping's database, against a real SQLite file."""

    def setUp(self):
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dbfile = Path(tmp.name) / "target.db"
        con = sqlite3.connect(self.dbfile)
        con.executescript("CREATE TABLE EMP (id INTEGER); CREATE TABLE dep_2 (id INTEGER); CREATE VIEW v AS SELECT * FROM EMP;")
        con.commit()
        con.close()
        self.conn = DatabaseConnection.objects.create(name="LOCAL", kind="sqlite", database=str(self.dbfile))

    def post(self, path, payload):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def test_browse_lists_tables_but_not_views_or_system_tables(self):
        body = self.post("/api/mappings/tables/", {"connection": self.conn.pk}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(sorted(t["table"] for t in body["tables"]), ["EMP", "dep_2"])
        self.assertEqual({t["schema"] for t in body["tables"]}, {"main"})
        self.assertFalse(body["truncated"])

    def test_verify_says_which_tables_exist(self):
        body = self.post("/api/mappings/verify/", {"connection": self.conn.pk, "tables": ["main.EMP", "emp", "DEP_2", "dbo.EMP", "nope", ""]}).json()
        got = {r["name"]: r["exists"] for r in body["results"]}
        self.assertEqual(got, {"main.EMP": True, "emp": True, "DEP_2": True, "dbo.EMP": False, "nope": False, "": False})
        self.assertEqual(body["results"][1]["found"], ["main.EMP"])

    def test_errors_are_plain(self):
        self.assertEqual(self.post("/api/mappings/tables/", {"connection": 999}).status_code, 400)
        self.assertEqual(self.post("/api/mappings/verify/", {"connection": self.conn.pk, "tables": "x"}).status_code, 400)
        oracle = DatabaseConnection.objects.create(name="O", kind="oracle", host="h", database="s", username="u")
        with mock.patch("loader.db_connect.driver_available", return_value=False):
            body = self.post("/api/mappings/tables/", {"connection": oracle.pk}).json()
        self.assertFalse(body["ok"])
        self.assertIn("pip install", body["message"])
        gone = DatabaseConnection.objects.create(name="G", kind="sqlite", database=str(self.dbfile) + ".missing")
        body = self.post("/api/mappings/tables/", {"connection": gone.pk}).json()
        self.assertFalse(body["ok"])
        self.assertIn("No file at", body["message"])

    def test_find_table_rules(self):
        from loader import mapping as m

        tables = [{"schema": "dbo", "table": "EMP"}, {"schema": "hr", "table": "EMP"}, {"schema": "dbo", "table": "DEP"}]
        self.assertEqual(len(m.find_table(tables, "emp")), 2)  # no schema: any schema
        self.assertEqual(len(m.find_table(tables, "HR.emp")), 1)
        self.assertEqual(len(m.find_table(tables, "Db.hr.emp")), 1)  # the database part is not compared
        self.assertEqual(m.find_table(tables, "x.EMP"), [])

    def test_create_table_flag_is_saved_per_rule(self):
        profile = DirectoryProfile.objects.create(name="P", stage_dir="/s", processing_dir="/p", archive_dir="/a", fail_dir="/f")
        rules = [{"pattern": "a*", "table": "A", "create_table": True}, {"pattern": "b*", "table": "B"}]
        made = self.post("/api/mappings/", {"profile": profile.pk, "connection": self.conn.pk, "rules": rules}).json()
        self.assertEqual([r["create_table"] for r in made["rules"]], [True, False])
        again = self.client.put(f"/api/mappings/{made['id']}/", data=json.dumps({"enabled": False}), content_type="application/json").json()
        self.assertEqual([r["create_table"] for r in again["rules"]], [True, False])  # kept when rules are not sent
        bad = self.post("/api/mappings/", {"profile": profile.pk, "connection": self.conn.pk, "rules": [{"pattern": "a*", "table": "A", "create_table": "yes"}]})
        self.assertEqual(bad.status_code, 400)


class IndexPageTests(TestCase):
    def test_bundle_urls_carry_a_build_stamp(self):
        html = self.client.get("/").content.decode()
        self.assertRegex(html, r"/static/app/app\.js\?v=\d+")
        self.assertRegex(html, r"/static/app/app\.css\?v=\d+")


class MappedLoadTests(TestCase):
    """Files go to the database and table the mapping rule names (real SQLite file as the target)."""

    def setUp(self):
        import sqlite3

        DirectoryProfile.objects.all().delete()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dbfile = self.root / "target.db"
        con = sqlite3.connect(self.dbfile)
        con.executescript("CREATE TABLE EMP (id INTEGER, name TEXT, hired DATE, salary REAL, active INTEGER);")
        con.commit()
        con.close()
        self.conn = DatabaseConnection.objects.create(name="LOCAL", kind="sqlite", database=str(self.dbfile))
        base = self.root / "p"
        self.profile = DirectoryProfile.objects.create(
            name="Hr", stage_dir=str(base / "stage"), processing_dir=str(base / "processing"),
            archive_dir=str(base / "archive"), fail_dir=str(base / "fail"),
        )
        self.lane = Lane(self.profile.pk, "Hr", base / "stage", base / "processing", base / "archive", base / "fail")
        self.cfg = EngineConfig(heartbeat=self.root / "hb", poll_interval=0.1, settle_seconds=0, probe_seconds=0,
                                batch_size=2, workers=1, max_attempts=3)
        self.engine = Engine(self.cfg, lanes=[self.lane])
        self.engine.prepare()
        self.worker = Worker(lambda: self.cfg, "w1")

    def rules(self, *rules, enabled=True):
        mapping = Mapping.objects.create(profile=self.profile, connection=self.conn, enabled=enabled)
        for n, (pattern, table, create, *keys) in enumerate(rules):
            MappingRule.objects.create(mapping=mapping, position=n, pattern=pattern, table=table, create_table=create,
                                       merge_keys=keys[0] if keys else "")

    def load(self, name, text):
        (self.lane.stage / name).write_text(text, encoding="utf-8")
        self.engine.run_once()
        self.worker.drain()
        return LoadJob.objects.get(filename=name)

    def query(self, sql):
        import sqlite3

        con = sqlite3.connect(self.dbfile)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_rows_land_in_the_mapped_table_not_in_dataloaders_own_database(self):
        self.rules(("emp*.*", "EMP", False))
        job = self.load("emp001.csv", "ID,Name,Hired,Salary,Active\n1,Ann,2020-01-31,10.5,true\n2,Bob,,20,0\n3,Cy,2021-02-03 10:30:00,,1\n")
        self.assertEqual((job.status, job.rows_loaded, job.target), (LoadJob.Status.LOADED, 3, "LOCAL → EMP"))
        self.assertEqual(self.query("SELECT id, name, hired, salary FROM EMP ORDER BY id"),
                         [(1, "Ann", "2020-01-31", 10.5), (2, "Bob", None, 20.0), (3, "Cy", "2021-02-03 10:30:00", None)])
        self.assertEqual(Record.objects.count(), 0)
        self.assertTrue(Path(job.final_path).parent == self.lane.archive)

    def test_json_nested_values_become_text(self):
        self.rules(("emp*.*", "EMP", False))
        job = self.load("emp9.json", '[{"id": 1, "name": {"first": "A"}}]')
        self.assertEqual(job.status, LoadJob.Status.LOADED)
        self.assertEqual(self.query("SELECT name FROM EMP"), [('{"first": "A"}',)])

    def test_each_file_name_goes_to_its_own_table(self):
        self.rules(("emp*.*", "EMP", False), ("dep*.*", "DEP", True))
        self.load("emp1.csv", "id,name\n1,Ann\n")
        job = self.load("dep_7.csv", "code,title\nA1,Sales\nB2,Ops\n")
        self.assertEqual(job.status, LoadJob.Status.LOADED)
        self.assertEqual(self.query("SELECT code, title FROM DEP ORDER BY code"), [("A1", "Sales"), ("B2", "Ops")])
        self.assertEqual(self.query("SELECT count(*) FROM EMP"), [(1,)])

    def test_create_table_infers_types_and_keeps_text_codes_as_text(self):
        self.rules(("dep*.*", "DEP", True))
        self.load("dep1.csv", "code,qty,price,since,seen,note\n007,5,1.5,2020-01-01,2020-01-01T10:00:00Z,x\n010,6,2,2020-02-01,2020-01-02 11:00:00,\n")
        types = {r[1]: r[2] for r in self.query("PRAGMA table_info(DEP)")}
        self.assertEqual(types, {"code": "TEXT", "qty": "INTEGER", "price": "REAL", "since": "TEXT", "seen": "TEXT", "note": "TEXT"})
        self.assertEqual(self.query("SELECT code, qty, price, seen, note FROM DEP ORDER BY code"),
                         [("007", 5, 1.5, "2020-01-01 10:00:00", "x"), ("010", 6, 2.0, "2020-01-02 11:00:00", None)])
        again = self.load("dep2.csv", "code,qty,price,since,seen,note\n020,1,1,2020-03-01,2020-03-01 00:00:00,y\n")  # reuses the table
        self.assertEqual(again.status, LoadJob.Status.LOADED)
        self.assertEqual(self.query("SELECT count(*) FROM DEP"), [(3,)])

    def test_missing_table_without_the_tick_fails_and_creates_nothing(self):
        self.rules(("dep*.*", "DEP", False))
        job = self.load("dep1.csv", "code\nA\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("table DEP doesn’t exist", job.error)
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name = 'DEP'"), [])
        self.assertEqual(job.attempts, 1)  # not retried: waiting won't create the table
        self.assertEqual(self.names(self.lane.fail)[0].endswith("_dep1.csv"), True)

    def names(self, folder):
        return sorted(p.name for p in folder.iterdir() if not p.name.endswith(ERROR_SIDECAR))

    def test_a_file_no_rule_matches_fails_with_a_clear_message(self):
        self.rules(("emp*.*", "EMP", False))
        job = self.load("job.csv", "a\n1\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("no rule", job.error)
        self.assertIn("job.csv", job.error)

    def test_unknown_columns_fail_and_name_them(self):
        self.rules(("emp*.*", "EMP", False))
        job = self.load("emp1.csv", "id,nickname\n1,Ann\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("nickname", job.error)
        self.assertIn("Table columns", job.error)

    def test_a_bad_row_rolls_back_every_batch(self):
        self.rules(("emp*.*", "EMP", False))
        # batch_size is 2, so rows 1-2 are already inserted when row 3 (a bad date) is read
        job = self.load("emp1.csv", "id,name,hired\n1,A,2020-01-01\n2,B,2020-01-02\n3,C,31/12/2020\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("row 3", job.error)
        self.assertEqual(self.query("SELECT count(*) FROM EMP"), [(0,)])

    def test_a_missing_database_file_fails_instead_of_retrying(self):
        self.rules(("emp*.*", "EMP", False))
        self.conn.database = str(self.root / "gone.db")  # sqlite refuses a missing file
        self.conn.save()
        job = self.load("emp1.csv", "id\n1\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)  # a missing file is a configuration error, not an outage; outages are retried
        self.assertIn("could not connect", job.error)

    def test_a_file_loaded_before_the_mapping_existed_is_not_a_duplicate_for_the_target(self):
        self.rules(("emp*.*", "EMP", False), enabled=False)
        first = self.load("emp1.csv", "id\n1\n")  # no mapping yet: stored in DataLoader's own database
        self.assertEqual(first.target, "")
        Mapping.objects.filter(profile=self.profile).update(enabled=True)
        second = self.load("emp2.csv", "id\n1\n")  # identical contents, but now headed for the mapped table
        self.assertEqual((second.status, second.target), (LoadJob.Status.LOADED, "LOCAL → EMP"))
        third = self.load("emp3.csv", "id\n1\n")  # identical again, same target: rejected
        self.assertEqual(third.status, LoadJob.Status.FAILED)
        self.assertIn("already loaded into LOCAL → EMP", third.error)

    def test_merge_updates_rows_with_an_existing_key_and_adds_the_rest(self):
        self.query_write("CREATE TABLE PEOPLE (id INTEGER PRIMARY KEY, name TEXT, city TEXT); INSERT INTO PEOPLE VALUES (1, 'Ann', 'Old'), (2, 'Bob', 'Keep');")
        self.rules(("emp*.*", "PEOPLE", False, "id"))
        job = self.load("emp1.csv", "id,name,city\n1,Ann B,New\n3,Cy,Rome\n")
        self.assertEqual((job.status, job.rows_loaded), (LoadJob.Status.LOADED, 2))
        self.assertEqual(self.query("SELECT id, name, city FROM PEOPLE ORDER BY id"),
                         [(1, "Ann B", "New"), (2, "Bob", "Keep"), (3, "Cy", "Rome")])

    def query_write(self, script):
        import sqlite3

        con = sqlite3.connect(self.dbfile)
        con.executescript(script)
        con.commit()
        con.close()

    def test_merge_keeps_the_last_row_when_a_key_repeats_and_works_across_batches(self):
        self.query_write("CREATE TABLE PEOPLE (id INTEGER PRIMARY KEY, name TEXT);")
        self.rules(("emp*.*", "PEOPLE", False, "ID"))  # batch size is 2; key name case does not matter
        job = self.load("emp1.csv", "id,name\n1,a\n1,b\n2,c\n1,d\n")
        self.assertEqual(job.status, LoadJob.Status.LOADED)
        self.assertEqual(job.rows_loaded, 4)
        self.assertEqual(self.query("SELECT id, name FROM PEOPLE ORDER BY id"), [(1, "d"), (2, "c")])

    def test_created_table_gets_a_primary_key_and_the_next_file_merges_into_it(self):
        self.rules(("emp*.*", "STAFF", True, "id"))
        self.load("emp1.csv", "id,name\n1,Ann\n2,Bob\n")
        info = {r[1]: (r[3], r[5]) for r in self.query("PRAGMA table_info(STAFF)")}
        self.assertEqual(info["id"], (1, 1))  # NOT NULL, primary key position 1
        job = self.load("emp2.csv", "id,name\n2,Bobby\n3,Cy\n")
        self.assertEqual(job.status, LoadJob.Status.LOADED)
        self.assertEqual(self.query("SELECT id, name FROM STAFF ORDER BY id"), [(1, "Ann"), (2, "Bobby"), (3, "Cy")])

    def test_merge_key_problems_fail_the_file_clearly(self):
        self.query_write("CREATE TABLE PEOPLE (id INTEGER PRIMARY KEY, name TEXT);")
        self.rules(("emp*.*", "PEOPLE", False, "code"))
        job = self.load("emp1.csv", "id,name\n1,a\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("merge key “code” is not a column of the file", job.error)
        Mapping.objects.all().delete()
        self.rules(("emp*.*", "PEOPLE", False, "id"))
        job = self.load("emp2.csv", "id,name\n1,a\n,b\n")
        self.assertEqual(job.status, LoadJob.Status.FAILED)
        self.assertIn("row 2: key column id is empty", job.error)
        self.assertEqual(self.query("SELECT count(*) FROM PEOPLE"), [(0,)])

    def test_sql_server_merge_runs_the_statements_in_order(self):
        """No SQL Server here: a recording stand-in checks the order of what would be sent."""
        from loader import db_connect, targets

        log = []

        class Cursor:
            def execute(self, sql, params=None):
                log.append(("execute", " ".join(sql.split())))
                self.rows = [("id", "int"), ("name", "nvarchar")] if "INFORMATION_SCHEMA" in sql else []

            def executemany(self, sql, rows):
                log.append(("executemany", " ".join(sql.split()), list(rows)))

            def fetchall(self):
                return self.rows

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                log.append(("commit",))

            def rollback(self):
                log.append(("rollback",))

            def close(self):
                pass

        sql_conn = DatabaseConnection.objects.create(name="SQLB", kind="sqlserver", host="h", database="d")
        mapping = Mapping.objects.create(profile=self.profile, connection=sql_conn)
        MappingRule.objects.create(mapping=mapping, position=0, pattern="emp*.*", table="dbo.emp", merge_keys="id")
        (self.lane.stage / "emp1.csv").write_text("id,name\n1,Ann\n2,Bob\n3,Cy\n")
        self.engine.run_once()
        with mock.patch.object(db_connect, "connect_for_load", return_value=Conn()):
            self.worker.drain()
        job = LoadJob.objects.get(filename="emp1.csv")
        self.assertEqual((job.status, job.rows_loaded, job.target), (LoadJob.Status.LOADED, 3, "SQLB → dbo.emp"))
        steps = [e[0] if e[0] != "execute" else e[1].split(" ")[0] + " " + e[1].split(" ")[1] for e in log]
        self.assertEqual(steps, ["SELECT COLUMN_NAME,", "SELECT TOP", "executemany", "MERGE [dbo].[emp]", "DELETE FROM",   # batch of 2 rows
                                 "executemany", "MERGE [dbo].[emp]", "DELETE FROM", "DROP TABLE", "commit"])
        self.assertEqual(log[2][2], [(1, "Ann"), (2, "Bob")])
        self.assertEqual(log[5][2], [(3, "Cy")])

    def test_an_unreachable_target_database_is_retried(self):
        from loader import targets

        self.rules(("emp*.*", "EMP", False))
        with mock.patch.object(targets, "load_file", side_effect=targets.TargetDatabaseError("could not connect to LOCAL")):
            job = self.load("emp1.csv", "id\n1\n")
        self.assertEqual(job.status, LoadJob.Status.QUEUED)
        self.assertIn("will retry", job.error)
        self.assertIsNotNone(job.next_attempt_at)

    def test_switched_off_mapping_uses_dataloaders_own_storage(self):
        self.rules(("emp*.*", "EMP", False), enabled=False)
        job = self.load("emp1.csv", "id\n1\n")
        self.assertEqual((job.status, job.target), (LoadJob.Status.LOADED, ""))
        self.assertEqual(Record.objects.count(), 1)

    def test_a_worker_that_lost_its_lease_rolls_the_target_back(self):
        self.rules(("emp*.*", "EMP", False))
        (self.lane.stage / "emp1.csv").write_text("id\n1\n2\n3\n")
        self.engine.run_once()
        job = self.worker.claim_next()
        LoadJob.objects.filter(pk=job.pk).update(worker="someone-else")  # another worker took it over
        self.worker.process(job)
        self.assertEqual(self.query("SELECT count(*) FROM EMP"), [(0,)])
        self.assertEqual(LoadJob.objects.get(pk=job.pk).status, LoadJob.Status.PROCESSING)


class TargetHelperTests(SimpleTestCase):
    def test_merge_statements(self):
        from loader import targets as t

        cols, keys = ["id", "name", "city"], ["id"]
        ms = t.write_plan(t.DIALECTS["sqlserver"], "[dbo].[emp]", cols, keys)
        self.assertEqual(ms.insert, "INSERT INTO #dl_stage ([id], [name], [city]) VALUES (%s, %s, %s)")
        self.assertEqual(ms.after_batch[0],
                         "MERGE [dbo].[emp] AS t USING #dl_stage AS s ON (t.[id] = s.[id]) "
                         "WHEN MATCHED THEN UPDATE SET t.[name] = s.[name], t.[city] = s.[city] "
                         "WHEN NOT MATCHED THEN INSERT ([id], [name], [city]) VALUES (s.[id], s.[name], s.[city]);")
        self.assertTrue(ms.prepare[0].startswith("SELECT TOP 0 [id], [name], [city] INTO #dl_stage FROM ("))
        self.assertEqual((ms.after_batch[1], ms.cleanup), ("DELETE FROM #dl_stage", ["DROP TABLE #dl_stage"]))
        odbc = t.Dialect("sqlserver", quote="[]", param="?", merge="tsql")
        self.assertIn("VALUES (?, ?, ?)", t.write_plan(odbc, "[dbo].[emp]", cols, keys).insert)
        self.assertNotIn("WHEN MATCHED", t.write_plan(odbc, "[dbo].[emp]", ["id"], ["id"]).after_batch[0])  # only keys: nothing to update

        ora = t.write_plan(t.DIALECTS["oracle"], '"DBO"."EMP"', ["ID", "NAME"], ["ID"])
        self.assertEqual(ora.insert, 'MERGE INTO "DBO"."EMP" t USING (SELECT :1 AS "ID", :2 AS "NAME" FROM dual) s ON (t."ID" = s."ID") '
                                     'WHEN MATCHED THEN UPDATE SET t."NAME" = s."NAME" WHEN NOT MATCHED THEN INSERT ("ID", "NAME") VALUES (s."ID", s."NAME")')
        pg = t.write_plan(t.DIALECTS["postgres"], '"public"."emp"', ["id", "name"], ["id"])
        self.assertTrue(pg.insert.endswith('ON CONFLICT ("id") DO UPDATE SET "name" = excluded."name"'))
        my = t.write_plan(t.DIALECTS["mysql"], "`emp`", ["id", "name"], ["id"])
        self.assertTrue(my.insert.endswith("ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)"))
        plain = t.write_plan(t.DIALECTS["sqlserver"], "[emp]", ["id"], [])
        self.assertEqual((plain.prepare, plain.after_batch), ([], []))
        with self.assertRaises(t.TargetError):
            t.write_plan(t.DIALECTS["vertica"], '"emp"', ["id"], ["id"])

    def test_create_table_sql_with_a_primary_key(self):
        from loader import targets as t

        d = t.DIALECTS["sqlserver"]
        self.assertEqual(t.create_sql(d, "[dbo].[emp]", [("id", "BIGINT NOT NULL"), ("name", "NVARCHAR(255)")], ["id"]),
                         "CREATE TABLE [dbo].[emp] (\n  [id] BIGINT NOT NULL,\n  [name] NVARCHAR(255),\n  PRIMARY KEY ([id])\n)")

    def test_type_inference(self):
        from loader import targets as t

        self.assertEqual([t.kind_of(v) for v in ["12", "-3", "007", "1.5", "1e3", "2020-01-01", "2020-01-01 10:00", "abc", "", None, True, 5, 2.5, "+15551234"]],
                         ["int", "int", "text", "float", "float", "date", "datetime", "text", None, None, "bool", "int", "float", "text"])
        self.assertEqual(t.merge_kinds({"int", "float"}), "float")
        self.assertEqual(t.merge_kinds({"int", "text"}), "text")
        self.assertEqual(t.merge_kinds({"date", "datetime"}), "datetime")
        self.assertEqual(t.merge_kinds(set()), "text")

    def test_column_names_are_made_safe_and_unique(self):
        from loader import targets as t

        taken = set()
        d = t.DIALECTS["oracle"]
        self.assertEqual([t.clean_name(n, d, taken) for n in ["First Name", "first name", "1st", "a-b"]], ["FIRST_NAME", "FIRST_NAME_2", "C_1ST", "A_B"])

    def test_dialect_sql(self):
        from loader import targets as t

        self.assertEqual(t.table_ref(t.DIALECTS["oracle"], None, "dbo", "emp"), '"DBO"."EMP"')
        self.assertEqual(t.table_ref(t.DIALECTS["sqlserver"], None, "dbo", "EMP"), "[dbo].[EMP]")
        self.assertEqual(t.table_ref(t.DIALECTS["mysql"], None, None, "EMP"), "`EMP`")
        self.assertEqual(t.table_ref(t.DIALECTS["postgres"], None, "Public", "Emp"), '"public"."emp"')
        self.assertEqual(t.DIALECTS["oracle"].marker(3), ":3")
        self.assertEqual(t.DIALECTS["sqlserver"].text_type(10), "NVARCHAR(255)")
        self.assertEqual(t.DIALECTS["sqlserver"].text_type(3000), "NVARCHAR(MAX)")
        self.assertEqual(t.DIALECTS["oracle"].text_type(600), "CLOB")

    def test_coerce(self):
        from loader import targets as t

        d = t.DIALECTS["oracle"]
        self.assertIsNone(t.coerce("  ", "int", d))
        self.assertEqual(t.coerce("true", "bool", d), 1)
        self.assertIs(t.coerce("true", "bool", t.DIALECTS["postgres"]), True)
        self.assertEqual(t.coerce("2020-01-01", "datetime", d).isoformat(), "2020-01-01T00:00:00")
        self.assertEqual(t.coerce("2020-01-01 00:00:00", "date", d).isoformat(), "2020-01-01")
        self.assertEqual(t.coerce({"a": 1}, "text", d), '{"a": 1}')
        with self.assertRaises(t.TargetError):
            t.coerce("31/12/2020", "date", d)
