import { useEffect, useState } from "react";
import { createDirectory, deleteDirectory, testDirectory, updateDirectory } from "../api.js";
import FormRow from "../components/FormRow.jsx";
import DirectoryView from "./DirectoryView.jsx";

const FOLDERS = [
  { key: "stage_dir", label: "Stage folder", sub: "stage", help: "Drop files here. The engine picks them up." },
  { key: "processing_dir", label: "Processing folder", sub: "processing", help: "Holds the one file being loaded right now." },
  { key: "archive_dir", label: "Archive folder", sub: "archive", help: "Files that loaded successfully are moved here." },
  { key: "fail_dir", label: "Fail folder", sub: "fail", help: "Files that failed are moved here with a note explaining why." },
];
const KEYS = ["name", "enabled", "max_parallel", ...FOLDERS.map((f) => f.key)];
const pick = (o) => Object.fromEntries(KEYS.map((k) => [k, o[k]]));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Loads the saved profile (if any), then hands off to the form. */
export default function DirectoryProfileView({ id, profiles, types, refreshKey, notify, onDirtyChange, onBack, onSaved, onDeleted, onUploaded }) {
  const existing = id === "new" ? null : profiles.list.find((p) => String(p.id) === id) ?? null;
  if (!profiles.loaded) return <p className="muted">Loading profiles…</p>;
  if (id !== "new" && !existing) {
    return (
      <p className="empty">
        This profile no longer exists.{" "}
        <button type="button" className="link" onClick={onBack}>Back to all profiles</button>
      </p>
    );
  }
  return (
    <>
      <ProfileForm
        existing={existing}
        notify={notify}
        onDirtyChange={onDirtyChange}
        onBack={onBack}
        onSaved={onSaved}
        onDeleted={onDeleted}
      />
      {existing && <DirectoryView profile={existing} types={types} refreshKey={refreshKey} onUploaded={onUploaded} />}
    </>
  );
}

function ProfileForm({ existing, notify, onDirtyChange, onBack, onSaved, onDeleted }) {
  const blank = { name: "", enabled: true, max_parallel: 0, stage_dir: "", processing_dir: "", archive_dir: "", fail_dir: "" };
  const [saved, setSaved] = useState(() => (existing ? pick(existing) : blank));
  const [draft, setDraft] = useState(saved);
  const [base, setBase] = useState("");
  const [errors, setErrors] = useState({});
  const [results, setResults] = useState(null); // outcome of the last Test folders: { ok, message, results }
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  const dirty = !same(draft, saved);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  function setValue(key, value) {
    setDraft((d) => ({ ...d, [key]: value }));
    setErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
    setResults(null); // an old result no longer describes what's in the form
  }

  function fillFromBase() {
    const root = base.trim().replace(/[\\/]+$/, "");
    if (!root) return;
    const sep = root.includes("\\") || /^[A-Za-z]:/.test(root) ? "\\" : "/";
    setDraft((d) => ({ ...d, ...Object.fromEntries(FOLDERS.map((f) => [f.key, `${root}${sep}${f.sub}`])) }));
    setErrors({});
    setResults(null);
  }

  async function onTest() {
    setTesting(true);
    setResults(null);
    try {
      setResults(await testDirectory(draft));
    } catch (e) {
      setResults({ ok: false, message: e.message, results: {} });
    } finally {
      setTesting(false);
    }
  }

  async function onSave(event) {
    event.preventDefault();
    setSaving(true);
    try {
      const body = pick(draft);
      const record = existing ? await updateDirectory(existing.id, body) : await createDirectory(body);
      const next = pick(record);
      setSaved(next);
      setDraft(next);
      setErrors({});
      notify?.(`Profile “${record.name}” saved.${record.enabled ? "" : " It is switched off, so the engine ignores it."}`, "ok");
      onDirtyChange?.(false);
      await onSaved?.(record);
    } catch (e) {
      if (e.body?.errors) {
        setErrors(e.body.errors);
        notify?.("The profile was not saved. Check the highlighted fields.", "bad");
      } else {
        notify?.(`Couldn’t save the profile: ${e.message}`, "bad");
      }
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!window.confirm(`Delete the profile “${existing.name}”?\n\nThe engine stops watching its stage folder. The folders and the files in them are not deleted, and load history is kept. This can’t be undone.`)) return;
    try {
      await deleteDirectory(existing.id);
      notify?.(`Profile “${existing.name}” deleted.`, "ok");
      onDirtyChange?.(false);
      await onDeleted?.();
    } catch (e) {
      notify?.(`Couldn’t delete the profile: ${e.message}`, "bad");
    }
  }

  const busy = saving || testing;
  const note = (key) => {
    const r = results?.results?.[key];
    if (!r) return null;
    return <p className={`field-note ${r.ok ? "field-note-ok" : "field-note-bad"}`}>{r.ok ? "✓ " : "✗ "}{r.message}</p>;
  };

  return (
    <form className="settings" onSubmit={onSave} noValidate autoComplete="off">
      <p>
        <button type="button" className="link" onClick={onBack}>← All profiles</button>
      </p>

      <fieldset className="settings-group">
        <legend>Profile</legend>
        <FormRow id="prof-name" label="Name" error={errors.name} help="How this profile appears in the tree and in load history.">
          <input id="prof-name" className="input input-path input-wide" value={draft.name} spellCheck={false} maxLength={64}
            aria-invalid={errors.name ? true : undefined} onChange={(e) => setValue("name", e.target.value)} />
        </FormRow>
        <FormRow id="prof-enabled" label="Engine" error={errors.enabled}>
          <label className="check">
            <input id="prof-enabled" type="checkbox" checked={draft.enabled} onChange={(e) => setValue("enabled", e.target.checked)} />
            <span>{draft.enabled ? "Watching this profile’s stage folder" : "Off: the engine ignores this profile"}</span>
          </label>
        </FormRow>
        <FormRow id="prof-parallel" label="Files at once" error={errors.max_parallel}
          help="How many files from this profile may load at the same time. 0 means no limit (as many as there are workers). 1 loads them one by one, oldest first.">
          <input id="prof-parallel" className="input input-num" type="number" min={0} max={64} value={draft.max_parallel}
            aria-invalid={errors.max_parallel ? true : undefined}
            onChange={(e) => setValue("max_parallel", e.target.value === "" ? "" : Number(e.target.value))} />
        </FormRow>
      </fieldset>

      <fieldset className="settings-group">
        <legend>Folders</legend>
        <FormRow id="prof-base" label="Base folder" help="Optional shortcut: fills the four folders below with stage, processing, archive and fail inside this folder.">
          <div className="inline">
            <input id="prof-base" className="input input-path input-wide" value={base} spellCheck={false} placeholder="C:\DataLoader\Sales"
              onChange={(e) => setBase(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); fillFromBase(); } }} />
            <button type="button" className="button button-quiet" disabled={!base.trim()} onClick={fillFromBase}>Fill folders</button>
          </div>
        </FormRow>
        {FOLDERS.map((f) => (
          <FormRow key={f.key} id={`prof-${f.key}`} label={f.label} error={errors[f.key]} help={f.help}>
            <input id={`prof-${f.key}`} className="input input-path input-wide" value={draft[f.key]} spellCheck={false}
              placeholder={`C:\\DataLoader\\Sales\\${f.sub}`}
              aria-invalid={errors[f.key] ? true : undefined} onChange={(e) => setValue(f.key, e.target.value)} />
            {note(f.key)}
          </FormRow>
        ))}
        <p className="field-help">All four folders must be different and on the same drive, so files move by atomic rename. Two profiles can’t share a stage or processing folder.</p>
      </fieldset>

      <section className="test-box" aria-live="polite">
        <button type="button" className="button button-quiet" disabled={busy} onClick={onTest}>
          {testing ? "Testing…" : "Test folders"}
        </button>
        {results && (
          <p className={`test-result ${results.ok ? "test-ok" : "test-bad"}`} role={results.ok ? "status" : "alert"}>
            <strong>{results.ok ? "Folders are usable." : "Folders need attention."}</strong> {results.message}
          </p>
        )}
        <p className="field-help">Checks the values above, even if they aren’t saved yet. Missing folders are created when the profile is saved and the engine next scans.</p>
      </section>

      <div className="settings-actions">
        <button type="submit" className="button" disabled={!dirty || busy}>
          {saving ? "Saving…" : existing ? "Save changes" : "Save profile"}
        </button>
        <button type="button" className="button button-quiet" disabled={!dirty || busy}
          onClick={() => { setDraft(saved); setErrors({}); setResults(null); }}>
          Discard changes
        </button>
        <span className="muted settings-state" role="status">
          {dirty ? "Unsaved changes" : existing ? "All changes saved" : ""}
        </span>
        {existing && (
          <button type="button" className="link settings-restore link-danger" disabled={busy} onClick={onDelete}>
            Delete profile
          </button>
        )}
      </div>
    </form>
  );
}
