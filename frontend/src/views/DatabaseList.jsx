import { useEffect, useRef, useState } from "react";
import { deleteDatabase, testDatabase } from "../api.js";

const TEST_AT_A_TIME = 3;

function serverOf(c) {
  if (c.kind === "sqlite") return c.database || "–";
  if (c.kind === "odbc" && c.options?.dsn) return `DSN ${c.options.dsn}`;
  if (!c.host) return "–";
  return c.port ? `${c.host}:${c.port}` : c.host;
}

/** The page for the Database branch: every saved connection, with Add, Test and Delete. */
export default function DatabaseList({ databases, notify, onAdd, onOpen, onReload }) {
  const { connections, kinds, loaded } = databases;
  const labels = Object.fromEntries(kinds.map((k) => [k.kind, k.label]));
  const [checked, setChecked] = useState(() => new Set());
  const [results, setResults] = useState({}); // id -> { state: "testing" | "ok" | "fail", message }
  const [busy, setBusy] = useState(false);
  const allBox = useRef(null);

  // Forget selections and results for connections that no longer exist.
  useEffect(() => {
    const ids = new Set(connections.map((c) => c.id));
    setChecked((prev) => new Set([...prev].filter((id) => ids.has(id))));
    setResults((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => ids.has(Number(id)))));
  }, [connections]);

  const selected = connections.filter((c) => checked.has(c.id));
  const allChecked = connections.length > 0 && selected.length === connections.length;

  useEffect(() => {
    if (allBox.current) allBox.current.indeterminate = selected.length > 0 && !allChecked;
  }, [selected.length, allChecked]);

  const toggle = (id) =>
    setChecked((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const toggleAll = () => setChecked(allChecked ? new Set() : new Set(connections.map((c) => c.id)));

  async function onTest() {
    const targets = [...selected];
    setBusy(true);
    setResults((prev) => ({ ...prev, ...Object.fromEntries(targets.map((c) => [c.id, { state: "testing" }])) }));
    const queue = [...targets];
    let passed = 0;
    const worker = async () => {
      while (queue.length) {
        const c = queue.shift();
        let outcome;
        try {
          const r = await testDatabase(c); // saved password is used when none is sent
          outcome = { state: r.ok ? "ok" : "fail", message: r.message };
        } catch (e) {
          outcome = {
            state: "fail",
            message: e.body?.errors ? "This connection has invalid settings. Open it to fix them." : e.message,
          };
        }
        if (outcome.state === "ok") passed += 1;
        setResults((prev) => ({ ...prev, [c.id]: outcome }));
      }
    };
    await Promise.all(Array.from({ length: Math.min(TEST_AT_A_TIME, targets.length) }, worker));
    setBusy(false);
    notify?.(
      targets.length === 1
        ? `${targets[0].name}: ${passed ? "connection works." : "connection failed."}`
        : `${passed} of ${targets.length} connections work.`,
      passed === targets.length ? "ok" : "bad"
    );
  }

  async function onDelete() {
    const targets = [...selected];
    const question =
      targets.length === 1
        ? `Delete the connection “${targets[0].name}”?`
        : `Delete these ${targets.length} connections?\n\n${targets.map((c) => c.name).join("\n")}`;
    if (!window.confirm(`${question}\n\nThis can’t be undone.`)) return;
    setBusy(true);
    const failed = [];
    for (const c of targets) {
      try {
        await deleteDatabase(c.id);
      } catch (e) {
        failed.push(`${c.name}: ${e.message}`);
      }
    }
    const done = targets.length - failed.length;
    if (done) notify?.(done === 1 ? "Connection deleted." : `${done} connections deleted.`, "ok");
    failed.forEach((f) => notify?.(`Couldn’t delete ${f}`, "bad"));
    setChecked(new Set());
    await onReload?.();
    setBusy(false);
  }

  if (!loaded) return <p className="muted">Loading connections…</p>;

  return (
    <>
      <div className="toolbar" role="toolbar" aria-label="Connection actions">
        <button type="button" className="button" onClick={onAdd} disabled={busy}>Add new connection</button>
        <button type="button" className="button button-quiet" onClick={onTest} disabled={busy || selected.length === 0}>
          {busy && selected.some((c) => results[c.id]?.state === "testing") ? "Testing…" : "Test connection"}
        </button>
        <button type="button" className="button button-quiet button-danger" onClick={onDelete} disabled={busy || selected.length === 0}>
          Delete connection
        </button>
        <span className="muted toolbar-note" role="status">
          {selected.length > 0
            ? `${selected.length} selected`
            : connections.length > 0
              ? "Tick one or more connections to test or delete them."
              : ""}
        </span>
      </div>

      {connections.length === 0 ? (
        <p className="empty">No connections yet. Choose Add new connection to save one.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col" className="col-check">
                  <input ref={allBox} type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="Select all connections" />
                </th>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Server</th>
                <th scope="col">Database</th>
                <th scope="col">User</th>
                <th scope="col">Last test</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((c) => {
                const r = results[c.id];
                return (
                  <tr key={c.id} className={checked.has(c.id) ? "row-selected" : ""}>
                    <td className="col-check">
                      <input type="checkbox" checked={checked.has(c.id)} onChange={() => toggle(c.id)} aria-label={`Select ${c.name}`} />
                    </td>
                    <td>
                      <button type="button" className="link" onClick={() => onOpen(c.id)} title="Open to edit">{c.name}</button>
                    </td>
                    <td>{labels[c.kind] ?? c.kind}</td>
                    <td className="break-cell">{serverOf(c)}</td>
                    <td>{c.kind === "sqlite" ? "–" : c.database || "–"}</td>
                    <td>{c.username || "–"}</td>
                    <td className="status-cell">
                      {!r && <span className="badge badge-muted">Not tested</span>}
                      {r?.state === "testing" && <span className="badge badge-processing">Testing…</span>}
                      {r?.state === "ok" && <span className="badge badge-loaded">Connected</span>}
                      {r?.state === "fail" && <span className="badge badge-failed">Failed</span>}
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
