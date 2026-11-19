import { useEffect, useRef, useState } from "react";
import { deleteDirectory, testDirectory } from "../api.js";

const TEST_AT_A_TIME = 3;

/** The page for the Directories branch: every profile, with Add, Test and Delete. */
export default function DirectoryList({ profiles, notify, onAdd, onOpen, onReload }) {
  const { list, loaded } = profiles;
  const [checked, setChecked] = useState(() => new Set());
  const [results, setResults] = useState({}); // id -> { state: "testing" | "ok" | "fail", message }
  const [busy, setBusy] = useState(false);
  const allBox = useRef(null);

  // Forget selections and results for profiles that no longer exist (keep state objects when nothing changed).
  useEffect(() => {
    const ids = new Set(list.map((p) => p.id));
    setChecked((prev) => (prev.size && [...prev].some((id) => !ids.has(id)) ? new Set([...prev].filter((id) => ids.has(id))) : prev));
    setResults((prev) => {
      const keep = Object.entries(prev).filter(([id]) => ids.has(Number(id)));
      return keep.length === Object.keys(prev).length ? prev : Object.fromEntries(keep);
    });
  }, [list]);

  const selected = list.filter((p) => checked.has(p.id));
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
  const toggleAll = () => setChecked(allChecked ? new Set() : new Set(list.map((p) => p.id)));

  async function onTest() {
    const targets = [...selected];
    setBusy(true);
    setResults((prev) => ({ ...prev, ...Object.fromEntries(targets.map((p) => [p.id, { state: "testing" }])) }));
    const queue = [...targets];
    let passed = 0;
    const worker = async () => {
      while (queue.length) {
        const p = queue.shift();
        let outcome;
        try {
          const r = await testDirectory(p);
          const bad = Object.values(r.results).find((x) => !x.ok);
          outcome = { state: r.ok ? "ok" : "fail", message: r.ok ? "" : bad?.message ?? r.message };
        } catch (e) {
          outcome = { state: "fail", message: e.message };
        }
        if (outcome.state === "ok") passed += 1;
        setResults((prev) => ({ ...prev, [p.id]: outcome }));
      }
    };
    await Promise.all(Array.from({ length: Math.min(TEST_AT_A_TIME, targets.length) }, worker));
    setBusy(false);
    notify?.(
      targets.length === 1
        ? `${targets[0].name}: ${passed ? "all four folders are usable." : "a folder needs attention."}`
        : `${passed} of ${targets.length} profiles have usable folders.`,
      passed === targets.length ? "ok" : "bad"
    );
  }

  async function onDelete() {
    const targets = [...selected];
    const question =
      targets.length === 1
        ? `Delete the profile “${targets[0].name}”?`
        : `Delete these ${targets.length} profiles?\n\n${targets.map((p) => p.name).join("\n")}`;
    if (!window.confirm(`${question}\n\nThe engine stops watching their stage folders. The folders and the files in them are not deleted, and load history is kept. This can’t be undone.`)) return;
    setBusy(true);
    const failed = [];
    for (const p of targets) {
      try {
        await deleteDirectory(p.id);
      } catch (e) {
        failed.push(`${p.name}: ${e.message}`);
      }
    }
    const done = targets.length - failed.length;
    if (done) notify?.(done === 1 ? "Profile deleted." : `${done} profiles deleted.`, "ok");
    failed.forEach((f) => notify?.(`Couldn’t delete ${f}`, "bad"));
    setChecked(new Set());
    await onReload?.();
    setBusy(false);
  }

  if (!loaded) return <p className="muted">Loading profiles…</p>;

  return (
    <>
      <div className="toolbar" role="toolbar" aria-label="Profile actions">
        <button type="button" className="button" onClick={onAdd} disabled={busy}>Add new profile</button>
        <button type="button" className="button button-quiet" onClick={onTest} disabled={busy || selected.length === 0}>
          {busy && selected.some((p) => results[p.id]?.state === "testing") ? "Testing…" : "Test directories"}
        </button>
        <button type="button" className="button button-quiet button-danger" onClick={onDelete} disabled={busy || selected.length === 0}>
          Delete profile
        </button>
        <span className="muted toolbar-note" role="status">
          {selected.length > 0
            ? `${selected.length} selected`
            : list.length > 0
              ? "Tick one or more profiles to test or delete them."
              : ""}
        </span>
      </div>

      {list.length === 0 ? (
        <p className="empty">No profiles yet, so the engine has nothing to watch. Choose Add new profile.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col" className="col-check">
                  <input ref={allBox} type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="Select all profiles" />
                </th>
                <th scope="col">Profile</th>
                <th scope="col">Engine</th>
                <th scope="col">Stage folder</th>
                <th scope="col" className="num">Waiting</th>
                <th scope="col" className="num">Archived</th>
                <th scope="col" className="num">Failed</th>
                <th scope="col">Last test</th>
              </tr>
            </thead>
            <tbody>
              {list.map((p) => {
                const r = results[p.id];
                return (
                  <tr key={p.id} className={checked.has(p.id) ? "row-selected" : ""}>
                    <td className="col-check">
                      <input type="checkbox" checked={checked.has(p.id)} onChange={() => toggle(p.id)} aria-label={`Select ${p.name}`} />
                    </td>
                    <td>
                      <button type="button" className="link" onClick={() => onOpen(p.id)} title="Open to edit">{p.name}</button>
                    </td>
                    <td>
                      {p.enabled ? <span className="badge badge-loaded">Watching</span> : <span className="badge badge-muted">Off</span>}
                    </td>
                    <td className="break-cell">{p.stage_dir}</td>
                    <td className="num">{p.counts.stage}</td>
                    <td className="num">{p.counts.archive}</td>
                    <td className="num">{p.counts.fail}</td>
                    <td className="status-cell">
                      {!r && <span className="badge badge-muted">Not tested</span>}
                      {r?.state === "testing" && <span className="badge badge-processing">Testing…</span>}
                      {r?.state === "ok" && <span className="badge badge-loaded">Usable</span>}
                      {r?.state === "fail" && <span className="badge badge-failed">Problem</span>}
                      {r?.message && r.state !== "testing" && <p className="status-msg">{r.message}</p>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
