import { useEffect, useRef, useState } from "react";
import { deleteMapping } from "../api.js";

/** The page for the Mapping branch: every mapping, with Add and Delete. */
export default function MappingList({ mappings, profiles, databases, notify, onAdd, onOpen, onReload }) {
  const { list, loaded } = mappings;
  const labels = Object.fromEntries(databases.kinds.map((k) => [k.kind, k.label]));
  const [checked, setChecked] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const allBox = useRef(null);

  useEffect(() => {
    const ids = new Set(list.map((m) => m.id));
    setChecked((prev) => (prev.size && [...prev].some((id) => !ids.has(id)) ? new Set([...prev].filter((id) => ids.has(id))) : prev));
  }, [list]);

  const selected = list.filter((m) => checked.has(m.id));
  const allChecked = list.length > 0 && selected.length === list.length;
  useEffect(() => {
    if (allBox.current) allBox.current.indeterminate = selected.length > 0 && !allChecked;
  }, [selected.length, allChecked]);

  const toggle = (id) =>
    setChecked((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const toggleAll = () => setChecked(allChecked ? new Set() : new Set(list.map((m) => m.id)));

  async function onDelete() {
    const targets = [...selected];
    const question =
      targets.length === 1
        ? `Delete the mapping of profile “${targets[0].profile_name}”?`
        : `Delete these ${targets.length} mappings?\n\n${targets.map((m) => m.profile_name).join("\n")}`;
    if (!window.confirm(`${question}\n\nThe profile, its files and the database connection are not touched.`)) return;
    setBusy(true);
    const failed = [];
    for (const m of targets) {
      try {
        await deleteMapping(m.id);
      } catch (e) {
        failed.push(`${m.profile_name}: ${e.message}`);
      }
    }
    const done = targets.length - failed.length;
    if (done) notify?.(done === 1 ? "Mapping deleted." : `${done} mappings deleted.`, "ok");
    failed.forEach((f) => notify?.(`Couldn’t delete ${f}`, "bad"));
    setChecked(new Set());
    await onReload?.();
    setBusy(false);
  }

  if (!loaded) return <p className="muted">Loading mappings…</p>;

  const unmapped = profiles.list.filter((p) => !list.some((m) => m.profile === p.id)).length;
  const hint =
    selected.length > 0
      ? `${selected.length} selected`
      : profiles.list.length === 0
        ? "Add a directory profile first (Directories)."
        : databases.connections.length === 0
          ? "Add a database connection first (Database)."
          : unmapped === 0
            ? "Every directory profile has a mapping."
            : list.length > 0
              ? "Tick one or more mappings to delete them."
              : "";

  return (
    <>
      <div className="toolbar" role="toolbar" aria-label="Mapping actions">
        <button type="button" className="button" onClick={onAdd} disabled={busy}>Add new mapping</button>
        <button type="button" className="button button-quiet button-danger" onClick={onDelete} disabled={busy || selected.length === 0}>
          Delete mapping
        </button>
        <span className="muted toolbar-note" role="status">{hint}</span>
      </div>

      {list.length === 0 ? (
        <p className="empty">No mappings yet. A mapping says which database a directory profile loads into, and which file names go to which table.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col" className="col-check">
                  <input ref={allBox} type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="Select all mappings" />
                </th>
                <th scope="col">Directory profile</th>
                <th scope="col">Database</th>
                <th scope="col">File name pattern</th>
                <th scope="col">Table</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {list.map((m) => (
                <tr key={m.id} className={checked.has(m.id) ? "row-selected" : ""}>
                  <td className="col-check">
                    <input type="checkbox" checked={checked.has(m.id)} onChange={() => toggle(m.id)} aria-label={`Select the mapping of ${m.profile_name}`} />
                  </td>
                  <td>
                    <button type="button" className="link" onClick={() => onOpen(m.id)} title="Open to edit">{m.profile_name}</button>
                  </td>
                  <td>
                    {m.connection_name} <span className="muted">({labels[m.connection_kind] ?? m.connection_kind})</span>
                  </td>
                  <td className="break-cell">
                    {m.rules.map((r, i) => <div key={i}><code>{r.pattern}</code></div>)}
                  </td>
                  <td className="break-cell">
                    {m.rules.map((r, i) => <div key={i}><code>{r.table}</code></div>)}
                  </td>
                  <td>
                    <span className={`badge ${m.enabled ? "badge-loaded" : "badge-muted"}`}>{m.enabled ? "On" : "Off"}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
