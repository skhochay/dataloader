import { useRef, useState } from "react";
import { uploadFile } from "../api.js";

export default function UploadZone({ profileId, profileName, types = [], onUploaded }) {
  const [over, setOver] = useState(false);
  const [results, setResults] = useState([]);
  const inputRef = useRef(null);

  async function send(fileList) {
    const out = [];
    for (const file of Array.from(fileList)) {
      try {
        await uploadFile(profileId, file);
        out.push({ ok: true, text: `${file.name} added to stage${profileName ? ` of ${profileName}` : ""}.` });
      } catch (e) {
        out.push({ ok: false, text: `${file.name}: ${e.message}` });
      }
    }
    setResults(out);
    onUploaded?.(out);
  }

  function onDrop(event) {
    event.preventDefault();
    setOver(false);
    if (event.dataTransfer.files.length) send(event.dataTransfer.files);
  }

  return (
    <section
      className={`drop ${over ? "drop-over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <div>
        <h2>Add files to stage</h2>
        <p>
          Drop files here or choose them.
          {types.length > 0 && <> Supported: {types.join(", ")}.</>}
        </p>
      </div>
      <button type="button" className="button" onClick={() => inputRef.current?.click()}>
        Choose files
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        accept={types.join(",")}
        onChange={(e) => { send(e.target.files); e.target.value = ""; }}
      />
      <ul className="drop-results" aria-live="polite">
        {results.map((r, i) => (
          <li key={i} className={r.ok ? "ok" : "bad"}>{r.text}</li>
        ))}
      </ul>
    </section>
  );
}
