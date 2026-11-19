import { useCallback, useEffect, useState } from "react";
import { getJob, retryJob } from "../api.js";
import { STATUS_LABEL, formatDate, formatDuration, formatSize } from "../format.js";

export default function JobDetail({ id, onClose, onChanged }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setJob(await getJob(id));
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function retry() {
    setBusy(true);
    try {
      await retryJob(id);
      await load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const columns = job?.sample_rows?.length ? Object.keys(job.sample_rows[0].data) : [];

  return (
    <div className="scrim" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Load details" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h2>{job ? job.filename : "Load details"}</h2>
          <button type="button" className="button button-quiet" onClick={onClose}>Close</button>
        </div>

        {error && <p className="notice notice-error" role="alert">{error}</p>}

        {job && (
          <>
            <p><span className={`badge badge-${job.status}`}>{STATUS_LABEL[job.status]}</span></p>
            <p>Loaded into: <strong>{job.target || "DataLoader’s own storage (no mapping)"}</strong></p>

            {job.error && (
              <div className="error-box">
                <h3>Why it failed</h3>
                <pre>{job.error}</pre>
              </div>
            )}

            {job.can_retry && (
              <p>
                <button type="button" className="button" disabled={busy} onClick={retry}>
                  {busy ? "Moving file…" : "Move back to its stage folder and retry"}
                </button>
              </p>
            )}

            <dl className="facts">
              <dt>Profile</dt><dd>{job.profile || "–"}</dd>
              <dt>Rows loaded</dt><dd>{job.rows_loaded.toLocaleString()}</dd>
              <dt>Size</dt><dd>{formatSize(job.size_bytes)}</dd>
              <dt>Started</dt><dd>{formatDate(job.started_at)}</dd>
              <dt>Took</dt><dd>{formatDuration(job.started_at, job.finished_at)}</dd>
              <dt>SHA-256</dt><dd className="break">{job.checksum || "–"}</dd>
              <dt>File location</dt><dd className="break">{job.final_path || "–"}</dd>
            </dl>

            {columns.length > 0 && (
              <>
                <h3>First {job.sample_rows.length} rows</h3>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col" className="num">#</th>
                        {columns.map((c) => <th scope="col" key={c}>{c}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {job.sample_rows.map((r) => (
                        <tr key={r.row_number}>
                          <td className="num">{r.row_number}</td>
                          {columns.map((c) => (
                            <td key={c}>{typeof r.data[c] === "object" ? JSON.stringify(r.data[c]) : String(r.data[c] ?? "")}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
