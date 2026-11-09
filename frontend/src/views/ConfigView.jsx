import { useCallback, useEffect, useMemo, useState } from "react";
import { getConfig, resetConfig, saveConfig } from "../api.js";

// Inputs hold text while typing; booleans stay booleans.
const toDraft = (fields) =>
  Object.fromEntries(fields.map((f) => [f.key, f.kind === "bool" ? f.value : String(f.value)]));

export default function ConfigView({ notify, onDirtyChange }) {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({});
  const [errors, setErrors] = useState({});
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);

  const apply = useCallback((next) => {
    setData(next);
    setDraft(toDraft(next.fields));
    setErrors({});
  }, []);

  useEffect(() => {
    getConfig().then(apply).catch((e) => setLoadError(e.message));
  }, [apply]);

  const saved = useMemo(() => (data ? toDraft(data.fields) : {}), [data]);
  const changed = useMemo(
    () => (data ? data.fields.filter((f) => draft[f.key] !== saved[f.key]) : []),
    [data, draft, saved]
  );
  const dirty = changed.length > 0;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const setValue = (key, value) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
  };

  async function onSave(event) {
    event.preventDefault();
    setSaving(true);
    try {
      apply(await saveConfig(Object.fromEntries(changed.map((f) => [f.key, draft[f.key]]))));
      notify?.("Settings saved. The engine applies them on its next scan.", "ok");
    } catch (e) {
      if (e.body?.errors) {
        setErrors(e.body.errors);
        notify?.("Settings were not saved. Check the highlighted fields.", "bad");
      } else {
        notify?.(`Couldn’t save settings: ${e.message}`, "bad");
      }
    } finally {
      setSaving(false);
    }
  }

  async function onRestoreAll() {
    if (!window.confirm("Restore every setting to its default?")) return;
    try {
      apply(await resetConfig());
      notify?.("All settings restored to their defaults.", "ok");
    } catch (e) {
      notify?.(`Couldn’t restore defaults: ${e.message}`, "bad");
    }
  }

  if (loadError) return <p className="notice notice-error" role="alert">{loadError}</p>;
  if (!data) return <p className="muted">Loading settings…</p>;

  const groups = [...new Set(data.fields.map((f) => f.group))];
  const anyCustom = data.fields.some((f) => f.custom);

  return (
    <form className="settings" onSubmit={onSave} noValidate>
      {groups.map((group) => (
        <fieldset className="settings-group" key={group}>
          <legend>{group}</legend>
          {data.fields.filter((f) => f.group === group).map((f) => (
            <Field
              key={f.key}
              field={f}
              value={draft[f.key]}
              error={errors[f.key]}
              onChange={(v) => setValue(f.key, v)}
            />
          ))}
        </fieldset>
      ))}

      <div className="settings-actions">
        <button type="submit" className="button" disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        <button type="button" className="button button-quiet" disabled={!dirty || saving} onClick={() => apply(data)}>
          Discard changes
        </button>
        <span className="muted settings-state" role="status">
          {dirty ? `${changed.length} unsaved ${changed.length === 1 ? "change" : "changes"}` : "All changes saved"}
        </span>
        <button type="button" className="link settings-restore" disabled={!anyCustom} onClick={onRestoreAll}>
          Restore all defaults
        </button>
      </div>

      <section className="settings-group">
        <h2 className="legend">Fixed</h2>
        {data.readonly.map((r) => (
          <div className="field" key={r.label}>
            <span className="field-label">{r.label}</span>
            <span className="break">{r.value}</span>
          </div>
        ))}
      </section>
    </form>
  );
}

function Field({ field, value, error, onChange }) {
  const id = `setting-${field.key}`;
  const describedBy = [field.help && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  const isDefault = String(value) === String(field.default);

  let control;
  if (field.kind === "bool") {
    control = (
      <label className="check">
        <input
          id={id}
          type="checkbox"
          checked={value}
          aria-describedby={describedBy}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>{value ? "On" : "Off"}</span>
      </label>
    );
  } else {
    control = (
      <span className="input-line">
        <input
          id={id}
          className={field.kind === "path" ? "input input-path" : "input input-num"}
          type={field.kind === "path" ? "text" : "number"}
          inputMode={field.kind === "path" ? undefined : "decimal"}
          step={field.kind === "int" ? 1 : "any"}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          value={value}
          spellCheck={false}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(e) => onChange(e.target.value)}
        />
        {field.unit && <span className="muted">{field.unit}</span>}
      </span>
    );
  }

  return (
    <div className={`field ${error ? "field-error" : ""}`}>
      <label className="field-label" htmlFor={id}>
        {field.label}
        {field.custom && <span className="tag">Customized</span>}
      </label>
      <div className="field-body">
        {control}
        {field.help && <p className="field-help" id={`${id}-help`}>{field.help}</p>}
        {error && <p className="field-msg" id={`${id}-error`} role="alert">{error}</p>}
        {!isDefault && (
          <button type="button" className="link field-default" onClick={() => onChange(field.kind === "bool" ? field.default : String(field.default))}>
            Use default ({String(field.default)})
          </button>
        )}
      </div>
    </div>
  );
}
