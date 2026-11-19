import { useState } from "react";
import { getDirectoryFiles } from "../api.js";
import UploadZone from "../components/UploadZone.jsx";
import { formatDate, formatSize } from "../format.js";
import { usePolling } from "../hooks.js";

const FOLDERS = [
  { id: "stage", label: "Stage", empty: "Nothing waiting. Files appear here until the engine picks them up." },
  { id: "processing", label: "Processing", empty: "Nothing is loading right now." },
  { id: "archive", label: "Archive", empty: "No archived files yet." },
  { id: "fail", label: "Fail", empty: "No failed files. Anything the engine can’t load shows up here." },
];

/** The files in one profile's folders, one tab per folder. Stage also takes uploads. */
export default function DirectoryView({ profile, refreshKey, types, onUploaded }) {
  const [folder, setFolder] = useState("stage");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const current = FOLDERS.find((f) => f.id === folder);
  const counts = profile.counts ?? {};

  usePolling(
    async () => {
      try {
        setData(await getDirectoryFiles(profile.id, folder));
        setError("");
      } catch (e) {
        setError(e.message);
      }
    },
    3000,
    [profile.id, folder, refreshKey]
  );

  return (
    <section className="files-panel" aria-label="Files in this profile">
      <div className="tabs" role="tablist" aria-label="Folders">
        {FOLDERS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={folder === f.id}
            className={`tab ${folder === f.id ? "tab-active" : ""}`}
            onClick={() => { setFolder(f.id); setData(null); }}
          >
            {f.label}
            <span className={`count ${f.id === "fail" && counts.fail > 0 ? "count-fail" : ""}`}>{counts[f.id] ?? 0}</span>
          </button>
        ))}
      </div>

      {folder === "stage" && <UploadZone profileId={profile.id} profileName={profile.name} types={types} onUploaded={onUploaded} />}
      {error && <p className="notice notice-error" role="alert">{error}</p>}
      {data && (
        <p className="path">
          Folder <code>{data.directory}</code>
          {data.total > data.files.length && <> (showing the newest {data.files.length} of {data.total})</>}
        </p>
      )}
      {data && data.files.length === 0 && <p className="empty">{current.empty}</p>}
      {data && data.files.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col" className="num">Size</th>
                <th scope="col">Modified</th>
              </tr>
            </thead>
            <tbody>
              {data.files.map((f) => (
                <tr key={f.name}>
                  <td>{f.name}</td>
                  <td className="num">{formatSize(f.size_bytes)}</td>
                  <td>{formatDate(f.modified_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
