import { useEffect, useMemo, useState } from "react";
import { createMapping, deleteMapping, getDirectoryFiles, getMappingTables, updateMapping } from "../api.js";
import FormRow from "../components/FormRow.jsx";
import { findTables, matchRule, suggestRules, tableLabel } from "../mapping.js";

/** Finds the saved mapping (if any), then hands off to the form. */
export default function MappingView({ id, mappings, profiles, databases, notify, onDirtyChange, onBack, onSaved, onDeleted }) {
  const existing = id === "new" ? null : mappings.list.find((m) => String(m.id) === id) ?? null;
  if (!mappings.loaded || !profiles.loaded || !databases.loaded) return <p className="muted">Loading…</p>;
  if (id !== "new" && !existing) {
    return (
      <p className="empty">
        This mapping no longer exists. <button type="button" className="link" onClick={onBack}>Back to all mappings</button>
      </p>
    );
  }
  return (
    <MappingForm
      existing={existing}
      mappings={mappings.list}
      profiles={profiles.list}
      connections={databases.connections}
      kinds={databases.kinds}
      notify={notify}
      onDirtyChange={onDirtyChange}
      onBack={onBack}
      onSaved={onSaved}
      onDeleted={onDeleted}
    />
  );
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let rowKey = 0;
const row = (pattern = "", table = "", create_table = false, merge_keys = "") => ({ k: ++rowKey, pattern, table, create_table, merge_keys });
const fromRule = (r) => row(r.pattern, r.table, r.create_table, r.merge_keys);
const plain = (d) => ({ ...d, rules: d.rules.map(({ pattern, table, create_table, merge_keys }) => ({ pattern, table, create_table, merge_keys })) }); // without the client-only keys
const ARCHIVE_PREFIX = /^\d{8}T\d{6}Z_job\d+_/; // the engine adds this when it moves a file to archive or fail

function MappingForm({ existing, mappings, profiles, connections, kinds, notify, onDirtyChange, onBack, onSaved, onDeleted }) {
  const labels = Object.fromEntries(kinds.map((k) => [k.kind, k.label]));
  const initial = useMemo(
    () =>
      existing
        ? { profile: existing.profile, connection: existing.connection, enabled: existing.enabled, rules: existing.rules.map(fromRule) }
        : { profile: "", connection: "", enabled: true, rules: [row()] },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );
  const [saved, setSaved] = useState(initial);
  const [draft, setDraft] = useState(initial);
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [files, setFiles] = useState({ state: "idle", names: [], total: 0, error: "" }); // the profile's files, read automatically
  const [dbTables, setDbTables] = useState({ state: "idle", tables: [], truncated: false, error: "" }); // the database's tables, read automatically
  const [filesKey, setFilesKey] = useState(0);
  const [tablesKey, setTablesKey] = useState(0);
  const [extra, setExtra] = useState("");
  const [picker, setPicker] = useState(null); // { rule, filter, schema, shown } while the table list is open under a rule
  const dirty = !same(plain(draft), plain(saved));
  const connection = connections.find((c) => c.id === draft.connection);
  const profile = profiles.find((p) => p.id === draft.profile);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Read the files of the chosen profile, so the rules can be checked against real names.
  useEffect(() => {
    if (draft.profile === "") {
      setFiles({ state: "idle", names: [], total: 0, error: "" });
      return undefined;
    }
    let live = true;
    setFiles((f) => ({ ...f, state: "loading", error: "" }));
    Promise.all(["stage", "archive", "fail"].map((folder) => getDirectoryFiles(draft.profile, folder)))
      .then((parts) => {
        if (!live) return;
        const seen = new Set();
        const names = [];
        for (const part of parts) {
          for (const f of part.files) {
            if (f.name.endsWith(".error.txt")) continue;
            const name = f.name.replace(ARCHIVE_PREFIX, "");
            if (!seen.has(name)) {
              seen.add(name);
              names.push(name);
            }
          }
        }
        setFiles({ state: "ready", names, total: parts.reduce((n, p) => n + p.total, 0), error: "" });
      })
      .catch((e) => live && setFiles({ state: "error", names: [], total: 0, error: e.message }));
    return () => {
      live = false;
    };
  }, [draft.profile, filesKey]);

  // Read the tables of the chosen database, so each rule's table can be checked as it is typed.
  useEffect(() => {
    if (draft.connection === "") {
      setDbTables({ state: "idle", tables: [], truncated: false, error: "" });
      return undefined;
    }
    let live = true;
    setDbTables({ state: "loading", tables: [], truncated: false, error: "" });
    getMappingTables(draft.connection)
      .then((r) => {
        if (!live) return;
        setDbTables(r.ok === false ? { state: "error", tables: [], truncated: false, error: r.message } : { state: "ready", tables: r.tables, truncated: r.truncated, error: "" });
      })
      .catch((e) => live && setDbTables({ state: "error", tables: [], truncated: false, error: e.message }));
    return () => {
      live = false;
    };
  }, [draft.connection, tablesKey]);

  const free = profiles.filter((p) => !mappings.some((m) => m.profile === p.id));
  const profileChoices = existing ? profiles.filter((p) => p.id === existing.profile) : free;

  function change(patch, errorKeys = []) {
    setDraft((d) => ({ ...d, ...patch }));
    setErrors((e) => (errorKeys.some((k) => e[k]) ? Object.fromEntries(Object.entries(e).filter(([k]) => !errorKeys.includes(k))) : e));
  }
  const setRules = (fn) => {
    setDraft((d) => ({ ...d, rules: fn(d.rules) }));
    setErrors((e) => Object.fromEntries(Object.entries(e).filter(([k]) => !k.startsWith("rules"))));
  };
  const setRule = (i, key, value) => setRules((rs) => rs.map((r, n) => (n === i ? { ...r, [key]: value } : r)));
  const move = (i, by) =>
    setRules((rs) => {
      const next = [...rs];
      [next[i], next[i + by]] = [next[i + by], next[i]];
      return next;
    });

  // Which rule takes each file, and what that means for each rule.
  const filled = draft.rules.map((r) => r.pattern.trim());
  const assigned = files.names.map((name) => ({ name, rule: matchRule(filled, name) }));
  const unmatched = assigned.filter((a) => a.rule === -1).map((a) => a.name);
  const suggestions = useMemo(
    () => suggestRules(unmatched, dbTables.state === "ready" ? dbTables.tables : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [unmatched.join("\n"), dbTables]
  );

  const schemaCounts = useMemo(() => {
    const counts = new Map();
    for (const t of dbTables.tables) counts.set(t.schema, (counts.get(t.schema) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [dbTables.tables]);
  const schemaSummary =
    schemaCounts.length === 0 || (schemaCounts.length === 1 && !schemaCounts[0][0])
      ? ""
      : ` in ${schemaCounts.length} schema${schemaCounts.length === 1 ? "" : "s"} (${schemaCounts.slice(0, 6).map(([s, n]) => `${s || "no schema"}: ${n}`).join(", ")}${schemaCounts.length > 6 ? ", …" : ""})`;

  function pick(t) {
    setRule(picker.rule, "table", tableLabel(t));
    setPicker(null);
  }

  function tableState(table) {
    if (!table.trim()) return { kind: "empty" };
    if (dbTables.state === "loading") return { kind: "checking" };
    if (dbTables.state !== "ready") return { kind: "unknown" };
    const found = findTables(dbTables.tables, table);
    if (found.length) return { kind: "exists", found };
    return dbTables.truncated ? { kind: "unknown" } : { kind: "missing" };
  }

  const payload = () => ({
    profile: draft.profile === "" ? null : Number(draft.profile),
    connection: draft.connection === "" ? null : Number(draft.connection),
    enabled: draft.enabled,
    rules: draft.rules.map(({ pattern, table, create_table, merge_keys }) => ({ pattern, table, create_table, merge_keys })),
  });

  async function onSave(event) {
    event.preventDefault();
    setSaving(true);
    try {
      const body = payload();
      const record = existing ? await updateMapping(existing.id, body) : await createMapping(body);
      const next = { profile: record.profile, connection: record.connection, enabled: record.enabled, rules: record.rules.map(fromRule) };
      setSaved(next);
      setDraft(next);
      setErrors({});
      notify?.(`Mapping for profile “${record.profile_name}” saved.`, "ok");
      onDirtyChange?.(false);
      await onSaved?.(record);
    } catch (e) {
      if (e.body?.errors) {
        setErrors(e.body.errors);
        notify?.("The mapping was not saved. Check the highlighted fields.", "bad");
      } else {
        notify?.(`Couldn’t save the mapping: ${e.message}`, "bad");
      }
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!window.confirm(`Delete the mapping of profile “${profile?.name}”?\n\nThe profile, its files and the database connection are not touched.`)) return;
    try {
      await deleteMapping(existing.id);
      notify?.("Mapping deleted.", "ok");
      onDirtyChange?.(false);
      await onDeleted?.();
    } catch (e) {
      notify?.(`Couldn’t delete the mapping: ${e.message}`, "bad");
    }
  }

  const noProfiles = !existing && free.length === 0;
  const noConnections = connections.length === 0;
  const extraNames = extra.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

  return (
    <form className="settings" onSubmit={onSave} noValidate autoComplete="off">
      <p>
        <button type="button" className="link" onClick={onBack}>← All mappings</button>
      </p>
      {noProfiles && (
        <p className="notice" role="status">
          {profiles.length === 0
            ? "There are no directory profiles yet. Add one under Directories first."
            : "Every directory profile already has a mapping. Open one from the list to change it."}
        </p>
      )}
      {noConnections && <p className="notice" role="status">There are no database connections yet. Add one under Database first.</p>}

      <fieldset className="settings-group">
        <legend>1. Where do the files go?</legend>
        <FormRow id="map-profile" label="Directory profile" error={errors.profile}
          help={existing ? "To map a different profile, delete this mapping and add a new one." : "The files of this profile are loaded. Profiles that already have a mapping are not listed."}>
          <select id="map-profile" className="input input-select" value={draft.profile} disabled={Boolean(existing)}
            aria-invalid={errors.profile ? true : undefined} onChange={(e) => change({ profile: e.target.value === "" ? "" : Number(e.target.value) }, ["profile"])}>
            {!existing && <option value="">Choose a profile…</option>}
            {profileChoices.map((p) => (
              <option key={p.id} value={p.id}>{p.name}{p.enabled ? "" : " (off)"}</option>
            ))}
          </select>
          {files.state === "loading" && <span className="muted">Reading its files…</span>}
          {files.state === "ready" && <span className="muted">{files.names.length === 0 ? "No files in its folders yet." : `${files.names.length} different file name${files.names.length === 1 ? "" : "s"} found.`}</span>}
          {files.state === "error" && <span className="muted">Couldn’t read its folders: {files.error}</span>}
        </FormRow>
        <FormRow id="map-connection" label="Database" error={errors.connection} help="One of your saved connections. Add or test them under Database.">
          <select id="map-connection" className="input input-select" value={draft.connection}
            aria-invalid={errors.connection ? true : undefined} onChange={(e) => change({ connection: e.target.value === "" ? "" : Number(e.target.value) }, ["connection"])}>
            <option value="">Choose a database…</option>
            {connections.map((c) => (
              <option key={c.id} value={c.id}>{c.name} ({labels[c.kind] ?? c.kind})</option>
            ))}
          </select>
          {dbTables.state === "loading" && <span className="muted" role="status">Reading its tables…</span>}
          {dbTables.state === "ready" && (
            <span className="muted" role="status">
              {dbTables.tables.length} table{dbTables.tables.length === 1 ? "" : "s"} found{schemaSummary}{dbTables.truncated ? ". The list is cut off, so “not found” can’t be trusted" : ""}.
            </span>
          )}
        </FormRow>
        {dbTables.state === "error" && (
          <p className="notice notice-error" role="alert">
            Couldn’t read the tables of {connection?.name}: {dbTables.error}{" "}
            <button type="button" className="link" onClick={() => setTablesKey((k) => k + 1)}>Try again</button>
          </p>
        )}
        <FormRow id="map-enabled" label="Mapping" error={errors.enabled}>
          <label className="check">
            <input id="map-enabled" type="checkbox" checked={draft.enabled} onChange={(e) => change({ enabled: e.target.checked }, ["enabled"])} />
            <span>{draft.enabled ? "On" : "Off: the engine ignores this mapping"}</span>
          </label>
        </FormRow>
      </fieldset>

      <fieldset className="settings-group">
        <legend>2. Which files go to which table?</legend>
        <p className="field-help">
          Each rule sends the files whose name fits the pattern to a table. <code>*</code> stands for any characters, <code>?</code> for one.
          A file uses the first rule that fits, so put specific patterns first. Example: <code>emp*.*</code> fits <code>emp001.json</code> and <code>EMP_2024.xml</code>.
        </p>
        {errors.rules && <p className="field-msg" role="alert">{errors.rules}</p>}

        <div className="rules" role="group" aria-label="Mapping rules">
          <div className="rules-head" aria-hidden="true">
            <span>File name pattern</span><span />
            <span>Table</span><span />
          </div>
          {draft.rules.map((r, i) => {
            const pe = errors[`rules.${i}.pattern`];
            const te = errors[`rules.${i}.table`];
            const ke = errors[`rules.${i}.merge_keys`];
            const mine = assigned.filter((a) => a.rule === i).map((a) => a.name);
            const fits = r.pattern.trim() ? files.names.filter((n) => matchRule([r.pattern.trim()], n) === 0).length : 0;
            const state = tableState(r.table);
            return (
              <div className="rule" key={r.k}>
                <div className="rule-cell">
                  <input className={`input input-path ${pe ? "input-bad" : ""}`} value={r.pattern} spellCheck={false} placeholder="emp*.*"
                    aria-label={`Rule ${i + 1} file name pattern`} aria-invalid={pe ? true : undefined}
                    onChange={(e) => setRule(i, "pattern", e.target.value)} />
                  {pe && <p className="field-msg" role="alert">{pe}</p>}
                </div>
                <span className="rule-arrow" aria-hidden="true">⇒</span>
                <div className="rule-cell">
                  <input className={`input input-path ${te ? "input-bad" : ""}`} value={r.table} spellCheck={false} placeholder="schema.table"
                    aria-label={`Rule ${i + 1} table`} aria-invalid={te ? true : undefined}
                    onChange={(e) => setRule(i, "table", e.target.value)} />
                  {te && <p className="field-msg" role="alert">{te}</p>}
                </div>
                <div className="rule-actions">
                  <button type="button" className="button button-quiet button-small" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move rule ${i + 1} up`}>↑</button>
                  <button type="button" className="button button-quiet button-small" disabled={i === draft.rules.length - 1} onClick={() => move(i, 1)} aria-label={`Move rule ${i + 1} down`}>↓</button>
                  <button type="button" className="button button-quiet button-small button-danger" disabled={draft.rules.length === 1}
                    onClick={() => setRules((rs) => rs.filter((_, n) => n !== i))} aria-label={`Remove rule ${i + 1}`}>Remove</button>
                </div>

                <div className="rule-more">
                  <button type="button" className="button button-quiet button-small" disabled={dbTables.state !== "ready"}
                    aria-label={`Pick the table for rule ${i + 1} from the list`} aria-expanded={picker?.rule === i}
                    title={dbTables.state === "ready" ? "Show every table of the database" : "Choose a database first. Its tables are read automatically."}
                    onClick={() => setPicker(picker?.rule === i ? null : { rule: i, filter: "", schema: "", shown: 200 })}>
                    {picker?.rule === i ? "Close list" : "Pick table…"}
                  </button>
                  {files.state === "ready" && r.pattern.trim() && (
                    <span className={mine.length ? "rule-files" : "rule-files muted"} title={mine.join("\n")}>
                      {mine.length
                        ? `${mine.length} file${mine.length === 1 ? "" : "s"} here: ${mine.slice(0, 3).join(", ")}${mine.length > 3 ? ", …" : ""}`
                        : fits
                          ? "An earlier rule already takes the files that fit."
                          : "No files fit this pattern right now."}
                    </span>
                  )}
                  <TableBadge state={state} database={connection?.name} />
                  {(state.kind === "missing" || r.create_table) && r.table.trim() && (
                    <label className="check check-small rule-create">
                      <input type="checkbox" checked={r.create_table} aria-label={`Rule ${i + 1}: create the table if it does not exist`}
                        onChange={(e) => setRule(i, "create_table", e.target.checked)} />
                      <span>Create the table if it does not exist</span>
                    </label>
                  )}
                  {state.kind === "missing" && !r.create_table && (
                    <span className="rule-warn">Without this tick, these files fail until the table exists.</span>
                  )}
                  <label className="rule-keys">
                    <span>Merge on key column(s)</span>
                    <input className={`input input-small ${ke ? "input-bad" : ""}`} value={r.merge_keys} spellCheck={false} placeholder="for example id"
                      aria-label={`Rule ${i + 1}: key columns for merge`} aria-invalid={ke ? true : undefined}
                      onChange={(e) => setRule(i, "merge_keys", e.target.value)} />
                  </label>
                  <span className="muted rule-keys-help">
                    {r.merge_keys.trim()
                      ? `Rows whose ${r.merge_keys.trim()} already exists in the table are updated; the others are added (MERGE).`
                      : "Empty: every row is added. Enter the key column(s) to update existing rows instead."}
                  </span>
                  {ke && <p className="field-msg" role="alert">{ke}</p>}
                </div>
                {picker?.rule === i && <TablePicker picker={picker} setPicker={setPicker} tables={dbTables.tables} schemas={schemaCounts} truncated={dbTables.truncated} database={connection?.name} onPick={pick} />}
              </div>
            );
          })}
        </div>
        <p className="test-box">
          <button type="button" className="button button-quiet" onClick={() => setRules((rs) => [...rs, row()])}>Add rule</button>
          <span className="muted">
            {dbTables.state === "ready" ? "Type a table name, or use Pick table… under a rule to see every table." : draft.connection === "" ? "Choose a database above and its tables are offered here." : ""}
          </span>
        </p>

        {suggestions.length > 0 && (
          <div className="suggest" aria-label="Suggested rules">
            <p className="suggest-title">
              <strong>{unmatched.length} file{unmatched.length === 1 ? "" : "s"} in this profile {unmatched.length === 1 ? "has" : "have"} no rule.</strong> Add one:
            </p>
            <div className="suggest-chips">
              {suggestions.map((s) => (
                <button key={s.pattern} type="button" className="chip" onClick={() => setRules((rs) => [...rs.filter((r) => r.pattern.trim() || r.table.trim()), row(s.pattern, s.table)])}>
                  <code>{s.pattern}</code> <span className="muted">{s.count} file{s.count === 1 ? "" : "s"}{s.table ? ` → ${s.table}` : ""}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </fieldset>

      <fieldset className="settings-group">
        <legend>3. Check the result</legend>
        {draft.profile === "" ? (
          <p className="muted">Choose a directory profile to see what happens to its files.</p>
        ) : files.state === "loading" ? (
          <p className="muted">Reading the files…</p>
        ) : files.state === "error" ? (
          <p className="notice notice-error" role="alert">{files.error}</p>
        ) : assigned.length === 0 ? (
          <p className="muted">There are no files in the stage, archive or fail folder of this profile yet. Type some file names below to try the rules.</p>
        ) : (
          <>
            <div className="table-wrap">
              <table aria-label="What happens to each file">
                <thead>
                  <tr><th scope="col">File</th><th scope="col">Uses rule</th><th scope="col">Loads into</th></tr>
                </thead>
                <tbody>
                  {assigned.slice(0, 100).map((a) => (
                    <tr key={a.name}>
                      <td>{a.name}</td>
                      {a.rule === -1 ? (
                        <td colSpan={2}><span className="badge badge-failed">No rule fits</span></td>
                      ) : (
                        <>
                          <td><span className="muted">{a.rule + 1}:</span> <code>{draft.rules[a.rule].pattern.trim()}</code></td>
                          <td><code>{draft.rules[a.rule].table.trim() || "–"}</code></td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted">
              {assigned.length > 100 ? `Showing the first 100 of ${assigned.length} file names. ` : ""}
              Files in the stage, archive and fail folders.{" "}
              <button type="button" className="link" onClick={() => setFilesKey((k) => k + 1)}>Read the folders again</button>
            </p>
          </>
        )}
        <details className="try-other">
          <summary>Try other file names</summary>
          <FormRow id="map-names" label="File names" help="One per line.">
            <textarea id="map-names" className="input input-area" rows={3} value={extra} spellCheck={false}
              placeholder={"emp001.json\ndep_3243.xml"} onChange={(e) => setExtra(e.target.value)} />
          </FormRow>
          {extraNames.length > 0 && (
            <ul className="try-results">
              {extraNames.map((name, i) => {
                const rule = matchRule(filled, name);
                return (
                  <li key={i}>
                    {name} → {rule === -1 ? <span className="badge badge-failed">No rule fits</span> : <><code>{draft.rules[rule].pattern.trim()}</code> ⇒ <code>{draft.rules[rule].table.trim() || "–"}</code></>}
                  </li>
                );
              })}
            </ul>
          )}
        </details>
      </fieldset>

      <div className="settings-actions">
        <button type="submit" className="button" disabled={!dirty || saving}>
          {saving ? "Saving…" : existing ? "Save changes" : "Save mapping"}
        </button>
        <button type="button" className="button button-quiet" disabled={!dirty || saving}
          onClick={() => { setDraft({ ...saved, rules: saved.rules.map(fromRule) }); setErrors({}); }}>
          Discard changes
        </button>
        <span className="muted settings-state" role="status">{dirty ? "Unsaved changes" : existing ? "All changes saved" : ""}</span>
        {existing && (
          <button type="button" className="link settings-restore link-danger" disabled={saving} onClick={onDelete}>Delete mapping</button>
        )}
      </div>
    </form>
  );
}

function TableBadge({ state, database }) {
  switch (state.kind) {
    case "exists":
      return (
        <span className="badge badge-loaded" title={state.found.map(tableLabel).join(", ")}>
          Table exists{state.found.length > 1 ? ` (${state.found.length} schemas)` : ""}
        </span>
      );
    case "missing":
      return <span className="badge badge-failed" title={`No table with this name in ${database}.`}>Table not found</span>;
    case "checking":
      return <span className="badge badge-muted">Checking…</span>;
    case "unknown":
      return <span className="badge badge-muted" title="The table list of the database isn’t available.">Can’t check</span>;
    default:
      return <span className="muted">Choose a table</span>;
  }
}

const STEP = 500;

/** Every table of the database under one rule, with a filter and a schema choice. */
function TablePicker({ picker, setPicker, tables, schemas, truncated, database, onPick }) {
  const f = picker.filter.trim().toLowerCase();
  const hits = tables.filter((t) => (!picker.schema || t.schema === picker.schema) && tableLabel(t).toLowerCase().includes(f));
  const shown = hits.slice(0, picker.shown);
  const set = (patch) => setPicker((p) => ({ ...p, ...patch }));
  return (
    <section className="picker" aria-label={`Tables in ${database}`}>
      <div className="picker-bar">
        <input className="input" value={picker.filter} placeholder="Filter, for example EMP" aria-label="Filter tables" spellCheck={false}
          onChange={(e) => set({ filter: e.target.value, shown: 200 })} autoFocus />
        {schemas.length > 1 && (
          <select className="input input-select" value={picker.schema} aria-label="Schema" onChange={(e) => set({ schema: e.target.value, shown: 200 })}>
            <option value="">All schemas ({tables.length})</option>
            {schemas.map(([s, n]) => (
              <option key={s} value={s}>{s || "(no schema)"} ({n})</option>
            ))}
          </select>
        )}
        <span className="muted">{hits.length === tables.length ? `All ${tables.length} tables` : `${hits.length} of ${tables.length} tables`} in {database}</span>
        <button type="button" className="link" onClick={() => setPicker(null)}>Close</button>
      </div>
      {tables.length === 0 ? (
        <p className="muted">This login sees no tables in {database}. Check the database name in the connection, and that the user may see the tables (SELECT or metadata permission).</p>
      ) : hits.length === 0 ? (
        <p className="muted">No table matches your filter.</p>
      ) : (
        <ul className="browse-list">
          {shown.map((t) => (
            <li key={tableLabel(t)}>
              <button type="button" className="browse-item" onClick={() => onPick(t)}>
                {t.schema && <span className="muted">{t.schema}.</span>}{t.table}
              </button>
            </li>
          ))}
        </ul>
      )}
      {hits.length > shown.length && (
        <p className="muted">
          Showing {shown.length} of {hits.length}.{" "}
          <button type="button" className="link" onClick={() => set({ shown: picker.shown + STEP })}>Show {Math.min(STEP, hits.length - shown.length)} more</button>
        </p>
      )}
      {truncated && <p className="muted">The database has more tables than the list can hold ({tables.length}). Type the name instead.</p>}
    </section>
  );
}
