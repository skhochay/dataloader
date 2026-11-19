import { STATUS_LABEL, formatDate, formatDuration } from "../format.js";

export default function JobsTable({ jobs, onSelect }) {
  if (jobs.length === 0) {
    return <p className="empty">No loads yet. Add a file above, or copy one into a profile’s stage folder.</p>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">File</th>
            <th scope="col">Profile</th>
            <th scope="col">Status</th>
            <th scope="col">Loaded into</th>
            <th scope="col" className="num">Rows</th>
            <th scope="col">Started</th>
            <th scope="col" className="num">Took</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>
                <button type="button" className="link" onClick={() => onSelect(job.id)}>
                  {job.filename}
                </button>
              </td>
              <td>{job.profile || "–"}</td>
              <td>
                <span className={`badge badge-${job.status}`}>{STATUS_LABEL[job.status]}</span>
                {job.attempts > 1 && <span className="status-msg"> attempt {job.attempts}</span>}
              </td>
              <td>{job.target || <span className="muted">DataLoader storage</span>}</td>
              <td className="num">{job.rows_loaded.toLocaleString()}</td>
              <td>{formatDate(job.started_at)}</td>
              <td className="num">{formatDuration(job.started_at, job.finished_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
