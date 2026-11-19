# Dataloader

File-drop loading engine plus a web dashboard. Files dropped in a profile's stage folder are loaded into a
database in one transaction; success moves the file to that profile's archive, failure moves it to its fail folder with a
`.error.txt` note. Stack: Python, Django 5, React 18 (Vite). Runs on Windows; data lives in `C:\DataLoader`.

## Commands (Windows)

```bat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
cd frontend && npm install && npm run build && cd ..   :: bundle goes to loader\static\app\
python manage.py runserver 8080                         :: dashboard http://localhost:8080
python manage.py run_engine                             :: the engine (or use the Start button)
python manage.py test                                   :: Django tests
python -m unittest loader.test_parsers                  :: parser tests, no database needed
cd frontend && npm run dev                              :: hot reload on :5173, proxies /api to :8080
```

The browser shows the built bundle, so rebuild the frontend after editing `frontend/src`.

## Layout

- `loader/engine.py`: the engine. One `Lane` (four folders) per enabled `DirectoryProfile`, re-read every scan; stage -> processing -> archive/fail, per-file transaction, stability
  probe (size and mtime across `PROBE_SECONDS`, plus a lock check), reloads settings every scan,
  heartbeat file with PID, stop-request file, crash recovery on start.
- `loader/worker.py`: the work queue and `Worker` (claim, lease, retry, per-profile limit, load). `loader/management/commands/run_worker.py`
  is the child-process entry point the engine spawns. The engine (`Engine.run_once`) only scans and enqueues; with `WORKERS=0` it runs a
  `Worker` inline instead.
- `loader/engine_control.py`: web-side start (detached subprocess), graceful stop (stop file), force stop (psutil).
- `loader/parsers.py`: one generator per format, registered in `PARSERS`. Add a format there.
  Supported: .csv .json .txt .avro .parquet .xml .tsv .xlsx .xls
- `loader/handlers.py`: `load_records()`, the legacy load into DataLoader's own `Record` table, used only for profiles without an enabled mapping.
- `loader/targets.py`: the mapped load. `load_file()` surveys the file (columns, types), connects with `db_connect.connect_for_load`, reads the catalog (`fetch_columns`), creates the table if allowed, inserts in batches in one target transaction, calls `before_commit` (lease check), commits. `Dialect` objects hold quoting, placeholders and type names per database (ODBC picks the dialect from the driver's DBMS name). Errors: `TargetError` (permanent), `NoMappingRuleError`, `TargetDatabaseError` (transient, retried). `LoadJob.target` (migration 0008) records where a file went. `MappingRule.merge_keys` (migration 0009, comma-separated) switches the write from INSERT to a merge: `write_plan()` builds per-dialect statements (`tsql` stage table + MERGE, `oracle` MERGE ... FROM dual, `on_conflict` for PostgreSQL/SQLite, `dup_key` for MySQL, none for Vertica/other ODBC = fail); duplicates of a key inside one batch are collapsed in Python (last wins). Only SQLite is tested here.
- `loader/config_schema.py`: editable engine settings (timing, batch size, duplicates; no folders).
- `loader/profiles.py`, `loader/views_directories.py`: directory profiles. Validation (`validate`, `test_folders`), listing, and the
  API `GET/POST /api/directories/`, `GET/PUT/DELETE /api/directories/<id>/`, `POST /api/directories/test/`,
  `GET /api/directories/<id>/files/<stage|processing|archive|fail>/`, `POST /api/directories/<id>/upload/`. Errors return `{errors: {field: message}}`.
- `loader/mapping.py`, `loader/views_mappings.py`: mappings (one per profile: a saved connection plus ordered `pattern => table` rules, first match
  wins, case-insensitive fnmatch on the file name). `resolve_rule(profile, filename)` returns `(mapping, rule)` (`(None, None)` = no enabled mapping, legacy load; `(mapping, None)` = no rule fits, the file fails); `resolve_target` returns `(connection, table)` or None.
  API `GET/POST /api/mappings/`, `GET/PUT/DELETE /api/mappings/<id>/`, `POST /api/mappings/match/`, `/tables/` (browse), `/verify/`. `MappingRule.create_table` makes the loader create a missing table; `db_connect.list_tables()` reads each database's catalog (TABLE_SQL). Errors use keys like `rules.0.table`.
  Models `Mapping` (profile OneToOne CASCADE, connection FK PROTECT) and `MappingRule`; migration 0006. Frontend: `views/MappingList.jsx`, `views/MappingView.jsx` (reads the profile's files and the database's tables itself, checks rules live), `mapping.js` (glob matching mirrored from the server, suggestions), tree branch in `tree.js`. Inside `.settings-group`, a block that is not a `.field` needs `clear: both` (the legend floats), as `.table-wrap` has.
- `loader/views.py`, `urls.py`: JSON API under `/api/`.
- `frontend/src/`: `App.jsx` (shell, polling, messages), `tree.js` (left tree), `components/`, `views/`.

## Conventions

- Settings changed on the Configuration page are stored in `EngineSetting`; env vars give the defaults.
- Folders live in `DirectoryProfile` rows, not settings. Migration 0004 creates the `Default` profile from `settings.DEFAULT_PROFILE`
  (or the folders edited on the old Configuration page). Tests that start an unpinned `Engine()` must delete that profile first.
- A profile's four folders must share one filesystem so moves are atomic renames. Profiles may not share stage/processing folders.
- `LoadJob.profile` is `SET_NULL`; `profile_name` keeps the name after a delete (retry is then refused).
- Close parser generators before moving a file (Windows cannot move an open file); the engine does this.
- Never call `os.kill` on Windows (it hard-terminates); use the stop file or psutil.
- Job claims use a conditional UPDATE, never `SELECT ... FOR UPDATE`, so they work on SQLite, PostgreSQL, SQL Server and Oracle alike.
  The LOADED update happens inside the load transaction, conditioned on `status=PROCESSING, worker=me`; losing it raises `LeaseLost`.
- Workers write `workers/worker-<slot>.json` (status) and read `worker-<slot>.stop` next to the heartbeat file.
- `DATALOADER_LEASE_SECONDS` (default 60) exists for tests and live checks only.

## Status

Run here (Linux sandbox, Django 5.2.19, Python 3): `python manage.py test` (142 tests, all pass, 3 skipped for missing optional
parsers), the esbuild bundle, and a browser run (Playwright) against the real Django server and a real engine process covering
profiles (add, test, upload, load into each profile's archive, switch off, delete). NOT run here: the Vite build itself
(esbuild was used), the `.avro`/`.parquet`/`.xls` parsers (3 skipped tests), and anything on Windows. **Run the tests on Windows first.**

### Directories (profiles)

Left-tree branch **Directories** works like **Database**: selecting it opens `views/DirectoryList.jsx` (table, checkboxes, Add new
profile / Test directories / Delete profile); each profile is a leaf opening `views/DirectoryProfileView.jsx` (form with a Base folder
shortcut and Test folders) with `views/DirectoryView.jsx` (tabs for the four folders, upload on Stage) underneath. The header's Add
files dialog asks which profile. Load history has a Profile column. Test results on the list page are held in component state only.

### Database connections (built, untested except where noted)

Left-tree branch **Database**: "Add connection" plus one leaf per saved connection. Types in dropdown order:
Oracle, SQL Server, Vertica, PostgreSQL, MySQL, SQLite, ODBC.

- `loader/db_kinds.py`: table describing each type (driver module, pip package, default port, fields, options).
  It drives both server validation and the browser form. Add a database here plus a `_connect_<kind>` in `db_connect.py`.
- `loader/db_connect.py`: one connect function per type, drivers imported only when used. `test_connection()` has a hard
  time limit, scrubs the password from errors, and names the missing `pip install`. SQLite probes `sqlite_master`
  because plain `SELECT 1` succeeds on a file that isn't a database.
- `loader/vault.py`: Fernet, key from `DATALOADER_VAULT_KEY` (falls back to `SECRET_KEY`). Passwords are never returned.
- `loader/views_databases.py`: `GET/POST /api/databases/`, `GET/PUT/DELETE /api/databases/<id>/`, `POST /api/databases/test/`.
  Errors return `{errors: {field: message}}`.
- Frontend: `tree.js` `buildTree()`, `views/DatabaseView.jsx`, wired in `App.jsx` (which also owns the unsaved-changes guard).
- The **Database** group in the tree is itself selectable (`selectable: true` in `tree.js`, handled in `NavTree.jsx`): clicking it
  opens `views/DatabaseList.jsx` (table, checkboxes, Add / Test / Delete) and expands it; clicking it again collapses it.
  Test results on that page are held in component state only, not saved.
- Browser check: bundle with esbuild and drive it with Playwright against a mock of `/api/` (done once for the list page, not kept in the repo).

Verified here: connector tests including real SQLite (`python -m unittest loader.test_db_connect`), the Django API tests,
the esbuild bundle and browser checks. NOT verified: any connection to
Oracle, SQL Server, Vertica, PostgreSQL, MySQL or ODBC (no drivers or servers in the sandbox). Test each against a real server.

## Scaling plan (queue and workers)

- **Phase A, done:** `QUEUED` jobs in `LoadJob` (path, archive/fail dirs, attempts, next_attempt_at, lease_until, worker; migration 0005),
  `ChecksumClaim`, atomic claim by conditional UPDATE, 60 s leases renewed every 10 s, retry with backoff, per-profile `max_parallel`,
  `Workers` and `Attempts per file` settings, child workers supervised by the engine (restart on death, requeue their job at once),
  force stop kills the children, Overview shows queue and workers. SQLite runs in WAL mode with IMMEDIATE transactions.
- **Phase B, core done:** files load into the mapped connection and table (`targets.py`), creating the table when the rule says so. Still to do: per-database bulk loaders (PostgreSQL and Vertica COPY, Oracle direct-path, SQL Server fast_executemany or BCP), a per-rule option for all-text columns, ALTER for new columns, and tests against real servers.
- **Phase C:** split large files into chunks that several workers load in parallel; throttling per target database.
- **Phase D:** run workers on other machines (same queue, shared storage), metrics, alerts.

Verified: unit tests (101), and a live run with a real server, 3 worker processes, 22 files across profiles, a worker killed mid-load
(file queued again and loaded), a profile limited to one file at a time (log shows no overlap), and a clean stop that takes the workers
with it. NOT verified: any non-SQLite database, and anything on Windows (process start flags and stop handling for workers).

## Security notes

The dashboard has no login. It can start and stop processes and edit settings, so put it behind authentication
before exposing it beyond a trusted network.
