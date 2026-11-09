# dataloader

A file-drop loading engine with a web dashboard.

```
            ┌────────── loaded ─────────▶  archive/
 stage/ ──▶ processing/
            └────────── failed ─────────▶  fail/   (+ <file>.error.txt)
```

Drop a `.csv`, `.json`, `.txt`, `.avro`, `.parquet`, `.xml`, `.tsv`, `.xlsx` or `.xls` file into a stage folder. The engine, a
separate long-running process, loads it into the database in one transaction.
Success moves the file to that profile's archive folder. Any failure moves it to its fail folder
with a note explaining why. Every attempt is recorded and visible in the dashboard.

The four folders belong to a **directory profile**, and you can define as many profiles as you like
(Directories page). One engine watches every enabled profile. A first run creates a profile named
**Default** in `C:\DataLoader\stage`, `processing`, `archive` and `fail`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser        # optional, for /admin/

cd frontend && npm install && npm run build && cd ..
```

## Run

Two processes:

```bash
python manage.py run_engine             # the engine (Ctrl+C or SIGTERM stops it cleanly)
python manage.py runserver 8080         # dashboard at http://localhost:8080
```

The folders are created when the engine scans. Try it with the Default profile:
`copy sample_data\orders.csv C:\DataLoader\stage\` (loads), then
`copy sample_data\broken_orders.csv C:\DataLoader\stage\` (fails, with the reason shown).

`python manage.py run_engine --once` processes whatever is in stage and exits,
which is handy for cron or CI.

Frontend development with hot reload: `cd frontend && npm run dev`, then open
http://localhost:5173 (API calls are proxied to Django on port 8080).

## Supported file types

| Type | Becomes rows as | Needs |
|---|---|---|
| `.csv`, `.tsv` | First line is the header; one row per line | built in |
| `.json` | One row per object in an array (or the single object) | built in |
| `.txt` | One row per non-empty line, stored as `{"text": "..."}` | built in |
| `.xml` | One row per child element of the root; attributes become `@name`, repeated tags become lists | `defusedxml` |
| `.xlsx`, `.xls` | First sheet; first non-empty row is the header | `openpyxl`, `xlrd` |
| `.avro` | One row per record | `fastavro` |
| `.parquet` | One row per record | `pyarrow` |

`pip install -r requirements.txt` installs all of them. A file whose library is missing is sent
to the fail folder with the install command in its error note. Dates become ISO text, decimals
become strings, binary values become base64, and NaN becomes null so the JSON column accepts them.
`.xml` and `.xls` are read fully into memory; the rest stream.

## The dashboard

A header (logo, Add files, Refresh, Admin), a navigation tree of objects on the left, a main
area that shows whatever is selected in the tree, and a footer with the message log and engine
status. Keyboard: arrow keys move through the tree, Right and Left expand and collapse,
Enter selects.

| Tree node | Main area shows |
|---|---|
| Overview | Engine control (status light, process ID, Start and Stop), folder counts and the latest loads |
| Directories | A table of profiles (name, engine on/off, stage folder, waiting, archived and failed counts) with Add new profile, Test directories and Delete profile buttons |
| Directories, then one entry per profile, or Add profile | The profile form (name, on/off, four folders, a Base folder shortcut, Test folders), and below it the files in that profile's Stage, Processing, Archive and Fail folders (Stage has the upload box) |
| Load history, All, Loaded, Failed, In progress | Load attempts; click one for rows, reason and retry |
| Database | A table of saved connections with Add new connection, Test connection and Delete connection buttons |
| Database, then one entry per connection, or Add connection | The form to add or edit one connection, with its own Test connection button |
| Mapping | A table of mappings (profile, database, patterns, tables, on/off) with Add new mapping and Delete mapping buttons |
| Mapping, then one entry per profile, or Add mapping | The mapping screen: profile and database, the pattern => table rules with live table checks, and a result table for every file |
| Configuration | Editable engine settings (see below) |

To add an object, add a node in `frontend/src/tree.js` and a branch for it in `App.jsx`.

## Starting and stopping the engine from the dashboard

The Overview page has an engine card: a status light (green running, red stopped, amber while
starting or stopping), the engine's process ID, when it started, and **Start** and **Stop** buttons.

- **Start** launches `manage.py run_engine` as a detached process, so it keeps running if the web
  server restarts. Its output goes to `engine.log` next to the heartbeat file. If it dies on
  startup, the error is shown.
- **Stop** asks the engine to stop. It finishes the file it is loading, then exits.
  If that takes more than 10 seconds, a **Force stop** link appears. Force stop ends the process
  immediately; the interrupted load rolls back and the file is queued again on the next start.
- The engine reports its process ID in its heartbeat file, so the card also works for an engine you
  started yourself from a terminal. A second engine refuses to start while one is running.
- If the engine runs under a supervisor that restarts it (NSSM, systemd with `Restart=always`),
  Stop will be undone by the supervisor. Stop it through the supervisor instead.

These buttons start and stop processes on the server, and the dashboard has no login. Put it
behind authentication before exposing it beyond a trusted network.

## Mapping: which database and table a profile loads into

A **mapping** ties one directory profile to one saved database connection (Database branch) and says which file names go
to which table. Open **Mapping**, choose **Add new mapping**, and work down three steps on one screen:

1. **Where do the files go?** Pick the directory profile and the database. The screen reads the profile's files (stage, archive
   and fail folders) and the database's tables by itself.
2. **Which files go to which table?** Each rule is a file name pattern and a table:

   | File name pattern | Table |
   |---|---|
   | `emp*.*` | `dbo.EMP` |
   | `dep*.*` | `delta.DEP` |

   - `emp001.json`, `emp002.json` and `EMP_2024.xml` all go to `dbo.EMP`; `dep_3243.xml` goes to `delta.DEP`.
   - `*` matches any characters (also none), `?` matches exactly one. Case does not matter. Patterns match the file name only.
   - **The first matching rule wins**, so put specific patterns above general ones. Use the arrows to reorder.
     Two rules may not use the same pattern.
   - Under each rule you see the files it takes, and whether the table exists in the database ("Table exists", "Table not found",
     or "Can't check" when the database can't be read). Click in a Table box to pick from the database's tables, or type a name.
     Tables are written `table`, `schema.table` or `database.schema.table`.
   - If the table is not found, a tick **Create the table if it does not exist** appears. A missing table without the tick shows a warning.
   - Files that no rule fits are listed as suggestions: one click adds a rule such as `emp*.*` (with the table of that name, if the database has one).
3. **Check the result.** A table shows every file and the rule and table it will use. "No rule fits" marks a file that would not be loaded.
   **Try other file names** tests names that are not in the folders yet.

- One mapping per profile. Deleting a profile deletes its mapping. A connection that a mapping uses can't be deleted
  until that mapping is.
- Code: `loader/mapping.py` (matching, validation, `resolve_rule` / `resolve_target`), API `GET/POST /api/mappings/`,
  `GET/PUT/DELETE /api/mappings/<id>/`, `POST /api/mappings/match/`, `POST /api/mappings/tables/` (reads the tables of a saved
  connection with a read-only catalog query; the driver for that database type must be installed), `POST /api/mappings/verify/`.
  The screen matches names in the browser with `frontend/src/mapping.js`, which mirrors the server's matching.

### What happens when a file is loaded

When a worker picks up a file, the profile's mapping decides everything (`loader/targets.py`):

| Situation | Result |
|---|---|
| Profile has **no mapping**, or the mapping is switched off | Rows go to DataLoader's own database (the old behaviour) |
| Mapping on, **no rule matches** the file name | The file **fails**: "no rule in the mapping … matches …" |
| Rule matches, table exists, **no key columns** on the rule | Rows are inserted. File columns are matched to table columns by name, case does not matter |
| Rule matches, table exists, **key columns** on the rule (e.g. `id`) | **Merge**: rows whose key already exists are updated, the others are added |
| Rule matches, table missing, **tick on** | `CREATE TABLE` from the file's columns (the key columns become the primary key), then load |
| Rule matches, table missing, tick off | The file **fails**: "table … doesn't exist" |
| File has a column the table doesn't | The file **fails** and names the column (tables are never altered) |
| Database can't be reached or drops the connection | Temporary: the file goes back to the queue and is retried with a delay (Attempts per file) |

- The insert is **one transaction on the target**: any error, a bad value, a lost lease, rolls back every row of that file.
  Batches of `BATCH_SIZE` rows (Configuration) are sent with `executemany`.
- The file is read twice: first to learn its columns (a bad file is rejected before the database is touched), then to insert.
- Empty text becomes NULL. Nested JSON/XML values (objects, lists) are stored as JSON text. A `.txt` file has one column, `text`.
- Dates must be ISO: `2024-12-31` or `2024-12-31 14:30:00` (a time zone is converted to UTC and dropped). Other formats fail the file with the row number.
- The jobs list and the load details show where each file went ("Loaded into").

**Merge** needs to know which column(s) identify a row, so enter them under the rule in **Merge on key column(s)** (`id`, or `dept, empno`).
Key columns must be in the file and are never updated; a row with an empty key fails the file. If a key appears twice in the file, the later row wins.
How it is written, per database: SQL Server (also through ODBC) loads each batch into a `#dl_stage` temp table and runs `MERGE`; Oracle runs
`MERGE INTO … USING (SELECT … FROM dual)` with array binds; PostgreSQL and SQLite use `INSERT … ON CONFLICT (keys) DO UPDATE`, MySQL uses
`ON DUPLICATE KEY UPDATE` (these three need a primary key or unique index on the key columns; tables DataLoader creates get one).
Vertica and ODBC data sources that are not SQL Server, Oracle, PostgreSQL or MySQL do not support merge yet: the file fails with that message.
The merge SQL for SQL Server and Oracle was checked only as generated text and a simulated run, not against a real server.

**Tables created by DataLoader** get one column per file field, typed from the values in that first file:
whole numbers `BIGINT`/`NUMBER(19)`, decimals `DOUBLE`, `true`/`false` (JSON) flags, ISO dates and timestamps,
and everything else text (`VARCHAR`/`NVARCHAR`/`VARCHAR2`, `CLOB`/`MAX` when long). Values with leading zeros (`007`) stay text.
Column names are made safe (`First Name` becomes `First_Name`; Oracle upper-cases, PostgreSQL lower-cases). No indexes; a primary key only when the rule has key columns.
**The types come from the first file only.** If the first file has no value like `007` in a column, the column becomes a number and
later `007`s are loaded as `7`. When types matter, create the table yourself and leave the tick off. The schema must already exist.

Known limits: Oracle and MySQL commit `CREATE TABLE` automatically, so a table created for a file that then fails stays (empty).
A crash in the instant between the target's COMMIT and the job being marked loaded would load the file twice on retry (use a unique key
if that matters). Inserts use `executemany` (array binds on Oracle, multi-row inserts on MySQL); PostgreSQL/Vertica `COPY`, SQL Server
`fast_executemany` and Oracle direct-path are later optimisations. **Only SQLite has been run against a real target here;**
Oracle, SQL Server, PostgreSQL, MySQL, Vertica and ODBC loads are written but need a test against your own server.

## Database connections

Click **Database** in the tree to see every saved connection in a table (name, type, server, database,
user, and the result of the last test). Tick one or more rows, then use **Test connection** to check them
(several are tested at once and each row shows Connected or Failed with the reason) or **Delete connection**
to remove them. **Add new connection** opens the form, and clicking a connection's name opens it for editing.

To add one, choose **Add new connection** or **Add connection** in the tree. Give it a name, pick the type, fill in
the fields, and press **Test connection** before saving. The test uses the values currently in the form,
saved or not, and shows the result inline: success with the server version and time taken, or the
driver's error message. Saved connections appear under **Database** in the tree.

| Type | Python driver | Install |
|---|---|---|
| Oracle | `oracledb` (thin mode, no Oracle client needed) | `pip install oracledb` |
| SQL Server | `pymssql` | `pip install pymssql` |
| Vertica | `vertica-python` | `pip install vertica-python` |
| PostgreSQL | `psycopg` | `pip install "psycopg[binary]"` |
| MySQL | `pymysql` | `pip install pymysql` |
| SQLite | built in (opened read-only) | none |
| ODBC | `pyodbc`, plus the ODBC driver for your database | `pip install pyodbc` |

- Install only the drivers you need. If one is missing, the form says so and shows the install command.
- **The server opens the connection**, so the host, port and firewall rules that matter are the server's.
- Passwords are stored encrypted (Fernet) using a key derived from `DATALOADER_VAULT_KEY`, or from
  `DJANGO_SECRET_KEY` if that isn't set. They are never sent back to the browser. Leave the password
  blank when editing to keep the saved one. If the key changes, saved passwords can't be read and must
  be entered again, so set a fixed `DATALOADER_VAULT_KEY` in production and keep it safe.
- For ODBC, enter either a DSN or a driver name. Use *Extra attributes* for anything else, for example
  `TrustServerCertificate=yes;`.
- A connection can't change type after it is saved. Delete it and add a new one.

Saved connections are not yet used as a load target: the engine still writes to the Django database.

## How the engine behaves

- **All or nothing per file.** One database transaction per file; a bad row on line
  9,000 leaves zero rows behind.
- **Probes each file first.** Before loading, the engine records the file's size and
  modified time, waits `PROBE_SECONDS`, and records them again. If either changed, or
  another process still has the file open (a Windows copy in progress), the file is
  skipped and checked again on the next scan. Files must also be older than
  `SETTLE_SECONDS`. Hidden files and `.part`, `.tmp` files are ignored.
- **Crash safe.** A file being loaded sits in `processing/`. If the engine or a worker dies, the
  transaction rolls back and the file is queued again (see Parallel loading below).
- **No double loads.** Files with the same SHA-256 as an already-loaded file are sent to
  fail (turn off with `DATALOADER_REJECT_DUPLICATES=0`).
- **Empty files and unsupported types** go to fail rather than being silently skipped.
- Archived and failed files are renamed `<UTC timestamp>_job<id>_<name>` so nothing is
  overwritten.
- Failed files can be retried from the dashboard, which moves them back to stage.

## Parallel loading (workers)

The engine is a **scanner plus a work queue**. The scanner finds ready files, moves each from stage to
processing, and adds a `queued` row to the load history. **Worker processes** take jobs from that queue and load
them. Set **Worker processes** on the Configuration page (`DATALOADER_WORKERS`, default 1):

| Workers | Behaviour |
|---|---|
| 0 | No child processes. The engine loads files itself, one at a time (the old behaviour). |
| 1 to 64 | The engine starts that many `manage.py run_worker` child processes and keeps them running. Changing the number takes effect on the next scan: extra workers finish their file and leave, missing ones start. |

How it stays safe with many workers:

- **Claiming is atomic.** A worker takes a job with one conditional update (queued to processing, only if still queued).
  Two workers can never hold the same job. This works on every database, with no `SKIP LOCKED` needed.
- **Leases.** A claimed job has a 60 second lease that its worker renews every 10 seconds. If a worker dies, the engine
  queues its job again at once; if the whole engine dies, jobs whose lease ran out are queued again, and on engine start
  everything still marked processing goes back to the queue.
- **Retries.** Temporary errors (lost connection, locked file) are retried with growing waits (5 s, 10 s, ... up to 5 min),
  up to **Attempts per file** (default 3). Permanent errors (bad row, unsupported type, duplicate content) go straight to fail.
- **Files at once** (per profile, 0 = no limit). Set 1 to load a profile's files one at a time, oldest first.
- **Identical files never load at the same time.** A checksum claim makes the second copy wait, then be rejected as a duplicate.
- **A lost lease rolls the load back**, so a slow worker whose job was given to someone else cannot also commit it.

The Overview page shows the queue depth and one line per worker (PID and the file it is loading). The Load history has a
**Queued** status and shows "attempt N" when a file needed more than one try.

**Database limits.** The default SQLite database has one writer at a time (it runs in WAL mode so reading and parsing still
overlap), so extra workers give little speed-up there. Real parallel loading needs PostgreSQL (or another server database); see
the plan in `CLAUDE.md`.

## Configuration

Engine settings can be edited on the dashboard's **Configuration** page: poll
interval, minimum file age, stability probe, batch size and duplicate rejection. Folders are not settings; they belong to directory profiles (below). Saved values
are stored in the database and applied by the running engine on its next scan, with no restart.
Values are validated first (numbers in range). A setting you never changed, or set back to its default, follows the environment
variable below.

### Directory profiles

A profile is a name, an on/off switch and four full folder paths. Saving validates that the four folders are
different, writable and on the same drive, and that no other profile uses the same stage or processing folder
(profiles may share an archive or fail folder). **Test folders** checks without saving or creating anything.
The engine reads the profile list on every scan, so adding, editing, switching off or deleting a profile takes
effect without a restart. A profile whose folders can't be created (a missing drive) is skipped and logged;
the others keep running. Load history records the profile of every load.

Changing or deleting a profile does not move or delete files; files in the old folders stay where they are.
On upgrade, the migration turns the folders you had before into a profile named **Default**.

Environment variables (defaults in `config/settings.py`):

| Variable | Default | Purpose |
|---|---|---|
| `DATALOADER_DATA_DIR` | `C:\DataLoader` | Folders of the first-run Default profile; also holds the heartbeat and log |
| `DATALOADER_POLL_INTERVAL` | `5` | Seconds between scans when idle |
| `DATALOADER_SETTLE_SECONDS` | `2` | Minimum age since last modification before a file is probed |
| `DATALOADER_PROBE_SECONDS` | `2` | Wait between the two size/modified-time checks |
| `DATALOADER_BATCH_SIZE` | `1000` | Rows per bulk insert |
| `DATALOADER_REJECT_DUPLICATES` | `1` | Reject identical file contents |
| `DATALOADER_WORKERS` | `1` | Worker processes (0 = the engine loads files itself) |
| `DATALOADER_MAX_ATTEMPTS` | `3` | Tries per file for temporary errors |
| `POSTGRES_DB` (+ `_USER`, `_PASSWORD`, `_HOST`, `_PORT`) | unset | Use PostgreSQL instead of SQLite |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | dev values | Set these in production |

The four directories of a profile must be on the same filesystem so moves are atomic renames.

## Making it load into your own tables

- **New file format:** write a generator yielding `(row_number, dict)` in `loader/parsers.py`
  and add its extension to `PARSERS`.
- **Your own models:** edit `load_records()` in `loader/handlers.py`. It runs inside the
  per-file transaction, so raise any exception to reject the whole file.

## Production notes

Use PostgreSQL and run the engine under a supervisor. On Windows, NSSM or Task Scheduler
("run at startup") works well for the engine; serve the web app with `waitress-serve --port=8080
config.wsgi:application` (gunicorn does not run on Windows). On Linux, systemd:

```ini
[Service]
WorkingDirectory=/srv/dataloader
ExecStart=/srv/dataloader/.venv/bin/python manage.py run_engine
Restart=always
```

Run only one engine. The dashboard has no login; put it behind your
own authentication before exposing it.

## Tests

```bash
python manage.py test
```
