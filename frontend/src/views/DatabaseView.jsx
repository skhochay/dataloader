import { useEffect, useMemo, useState } from "react";
import { createDatabase, deleteDatabase, testDatabase, updateDatabase } from "../api.js";
import FormRow from "../components/FormRow.jsx";

/** Loads the saved connection (if any), then hands off to the form. */
export default function DatabaseView({ id, databases, notify, onDirtyChange, onReload, onBack, onSaved, onDeleted }) {
  const { connections, kinds, loaded } = databases;
  const existing = id === "new" ? null : connections.find((c) => String(c.id) === id) ?? null;

  if (!loaded) return <p className="muted">Loading connections…</p>;
  if (id !== "new" && !existing) {
    return (
      <p className="empty">
        This connection no longer exists.{" "}
        <button type="button" className="link" onClick={onBack}>Back to all connections</button>
      </p>
    );
  }
  if (kinds.length === 0) return <p className="notice notice-error" role="alert">No database types were returned by the server.</p>;

  return (
    <ConnectionForm
      existing={existing}
      kinds={kinds}
      notify={notify}
      onDirtyChange={onDirtyChange}
      onReload={onReload}
      onBack={onBack}
      onSaved={onSaved}
      onDeleted={onDeleted}
    />
  );
}

function blankFor(spec, name = "") {
  const options = {};
  for (const o of spec.options) options[o.key] = o.default ?? (o.type === "bool" ? false : "");
  return { name, kind: spec.kind, host: "", port: "", database: "", username: "", password: "", clear_password: false, options };
}

function fromSaved(conn, spec) {
  const draft = blankFor(spec, conn.name);
  return {
    ...draft,
    host: conn.host ?? "",
    port: conn.port == null ? "" : String(conn.port),
    database: conn.database ?? "",
    username: conn.username ?? "",
    options: { ...draft.options, ...(conn.options || {}) },
  };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function ConnectionForm({ existing, kinds, notify, onDirtyChange, onReload, onBack, onSaved, onDeleted }) {
  const specOf = (kind) => kinds.find((k) => k.kind === kind) ?? kinds[0];
  const initial = useMemo(
    () => (existing ? fromSaved(existing, specOf(existing.kind)) : blankFor(kinds[0])),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );
  const [saved, setSaved] = useState(initial);
  const [draft, setDraft] = useState(initial);
  const [hasPassword, setHasPassword] = useState(existing?.has_password ?? false);
  const [errors, setErrors] = useState({});
  const [result, setResult] = useState(null); // outcome of the last Test connection
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  const spec = specOf(draft.kind);
  const dirty = !same(draft, saved);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  function setValue(key, value) {
    setDraft((d) => ({ ...d, [key]: value }));
    setErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
    setResult(null); // an old result no longer describes what's in the form
  }

  function setOption(key, value) {
    setDraft((d) => ({ ...d, options: { ...d.options, [key]: value } }));
    setErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
    setResult(null);
  }

  function changeKind(kind) {
    setDraft((d) => blankFor(specOf(kind), d.name));
    setErrors({});
    setResult(null);
  }

  const payload = () => ({
    id: existing?.id,
    name: draft.name,
    kind: draft.kind,
    host: draft.host,
    port: draft.port,
    database: draft.database,
    username: draft.username,
    password: draft.password,
    clear_password: draft.clear_password,
    options: draft.options,
  });

  async function onTest() {
    setTesting(true);
    setResult(null);
    try {
      setResult(await testDatabase(payload()));
    } catch (e) {
      if (e.body?.errors) {
        setErrors(e.body.errors);
        setResult({ ok: false, message: "Fix the highlighted fields, then test again." });
      } else {
        setResult({ ok: false, message: e.message });
      }
    } finally {
      setTesting(false);
      onReload?.(); // refreshes the "driver installed" flags
    }
  }

  async function onSave(event) {
    event.preventDefault();
    setSaving(true);
    try {
      const body = payload();
      const record = existing ? await updateDatabase(existing.id, body) : await createDatabase(body);
      const next = { ...fromSaved(record, specOf(record.kind)) };
      setSaved(next);
      setDraft(next);
      setHasPassword(record.has_password);
      setErrors({});
      notify?.(`Connection “${record.name}” saved.`, "ok");
      onDirtyChange?.(false);
      await onSaved?.(record);
    } catch (e) {
      if (e.body?.errors) {
        setErrors(e.body.errors);
        notify?.("The connection was not saved. Check the highlighted fields.", "bad");
      } else {
        notify?.(`Couldn’t save the connection: ${e.message}`, "bad");
      }
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!window.confirm(`Delete the connection “${existing.name}”? This can’t be undone.`)) return;
    try {
      await deleteDatabase(existing.id);
      notify?.(`Connection “${existing.name}” deleted.`, "ok");
      onDirtyChange?.(false);
      await onDeleted?.();
    } catch (e) {
      notify?.(`Couldn’t delete the connection: ${e.message}`, "bad");
    }
  }

  const fields = spec.fields.filter((f) => f.name !== "password");
  const hasPasswordField = spec.fields.some((f) => f.name === "password");
  const busy = saving || testing;

  return (
    <form className="settings" onSubmit={onSave} noValidate autoComplete="off">
      <p>
        <button type="button" className="link" onClick={onBack}>← All connections</button>
      </p>
      {!spec.available && (
        <p className="notice" role="status">
          The {spec.label} driver isn’t installed on the server, so testing will fail until it is. Run{" "}
          <code>pip install {spec.package}</code> in the project’s environment, then press Test connection again.
        </p>
      )}

      <fieldset className="settings-group">
        <legend>Connection</legend>
        <FormRow id="db-name" label="Name" error={errors.name} help="How this connection appears in the tree.">
          <input id="db-name" className="input input-path" value={draft.name} spellCheck={false} maxLength={64}
            aria-invalid={errors.name ? true : undefined} onChange={(e) => setValue("name", e.target.value)} />
        </FormRow>
        <FormRow id="db-kind" label="Database type" error={errors.kind}
          help={existing ? "To change the type, delete this connection and add a new one." : undefined}>
          <select id="db-kind" className="input input-select" value={draft.kind} disabled={Boolean(existing)}
            onChange={(e) => changeKind(e.target.value)}>
            {kinds.map((k) => (
              <option key={k.kind} value={k.kind}>{k.label}</option>
            ))}
          </select>
        </FormRow>

        {fields.map((f) => (
          <FormRow key={f.name} id={`db-${f.name}`} label={f.label + (f.required ? "" : " (optional)")} error={errors[f.name]} help={f.help}>
            <input
              id={`db-${f.name}`}
              className={f.name === "port" ? "input input-num" : "input input-path"}
              type={f.name === "port" ? "number" : "text"}
              min={f.name === "port" ? 1 : undefined}
              max={f.name === "port" ? 65535 : undefined}
              placeholder={f.name === "port" && spec.default_port ? String(spec.default_port) : undefined}
              value={draft[f.name]}
              spellCheck={false}
              autoComplete="off"
              aria-invalid={errors[f.name] ? true : undefined}
              onChange={(e) => setValue(f.name, e.target.value)}
            />
          </FormRow>
        ))}

        {hasPasswordField && (
          <FormRow id="db-password" label="Password" error={errors.password}
            help={hasPassword && !draft.clear_password ? "A password is saved, encrypted. Leave this blank to keep it." : undefined}>
            <input
              id="db-password"
              className="input input-path"
              type="password"
              autoComplete="new-password"
              value={draft.password}
              placeholder={hasPassword && !draft.clear_password ? "••••••••  (saved)" : ""}
              disabled={draft.clear_password}
              onChange={(e) => setValue("password", e.target.value)}
            />
            {hasPassword && (
              <label className="check check-small">
                <input type="checkbox" checked={draft.clear_password}
                  onChange={(e) => setDraft((d) => ({ ...d, clear_password: e.target.checked, password: "" }))} />
                <span>Remove the saved password</span>
              </label>
            )}
          </FormRow>
        )}
      </fieldset>

      {spec.options.length > 0 && (
        <fieldset className="settings-group">
          <legend>Options</legend>
          {spec.options.map((o) => (
            <FormRow key={o.key} id={`db-opt-${o.key}`} label={o.label} error={errors[o.key]} help={o.help}>
              <OptionInput option={o} value={draft.options[o.key]} onChange={(v) => setOption(o.key, v)} />
            </FormRow>
          ))}
        </fieldset>
      )}

      <section className="test-box" aria-live="polite">
        <button type="button" className="button button-quiet" disabled={busy} onClick={onTest}>
          {testing ? "Testing…" : "Test connection"}
        </button>
        {testing && <span className="muted">Connecting. This can take up to 15 seconds.</span>}
        {result && (
          <p className={`test-result ${result.ok ? "test-ok" : "test-bad"}`} role={result.ok ? "status" : "alert"}>
            <strong>{result.ok ? "Connection works." : "Connection failed."}</strong> {result.message}
          </p>
        )}
        <p className="field-help">Tests the values above, even if they aren’t saved yet. The server opens the connection, so it uses the server’s network access.</p>
      </section>

      <div className="settings-actions">
        <button type="submit" className="button" disabled={!dirty || busy}>
          {saving ? "Saving…" : existing ? "Save changes" : "Save connection"}
        </button>
        <button type="button" className="button button-quiet" disabled={!dirty || busy}
          onClick={() => { setDraft(saved); setErrors({}); setResult(null); }}>
          Discard changes
        </button>
        <span className="muted settings-state" role="status">
          {dirty ? "Unsaved changes" : existing ? "All changes saved" : ""}
        </span>
        {existing && (
          <button type="button" className="link settings-restore link-danger" disabled={busy} onClick={onDelete}>
            Delete connection
          </button>
        )}
      </div>
    </form>
  );
}

function OptionInput({ option, value, onChange }) {
  const id = `db-opt-${option.key}`;
  if (option.type === "bool") {
    return (
      <label className="check">
        <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span>{value ? "On" : "Off"}</span>
      </label>
    );
  }
  if (option.type === "select") {
    return (
      <select id={id} className="input input-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {option.choices.map((c) => (
          <option key={c.value} value={c.value}>{c.label}</option>
        ))}
      </select>
    );
  }
  if (option.type === "textarea") {
    return <textarea id={id} className="input input-area" rows={3} value={value} spellCheck={false} onChange={(e) => onChange(e.target.value)} />;
  }
  return <input id={id} className="input input-path" value={value} spellCheck={false} onChange={(e) => onChange(e.target.value)} />;
}
