import { useEffect, useState } from "react";
import { formatTime } from "../format.js";

export default function Footer({ stats, serverDown, messages, updatedAt, onClear }) {
  const [open, setOpen] = useState(false);
  const latest = messages[messages.length - 1];

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const status = serverDown ? "down" : stats == null ? "checking" : stats.engine_online ? "on" : "off";
  const label = { down: "Server unreachable", checking: "Checking engine", on: stats?.engine?.pid ? `Engine running, PID ${stats.engine.pid}` : "Engine running", off: "Engine offline" }[status];

  return (
    <footer className="footer">
      <div className="footer-msg">
        <button type="button" className="msg-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          Messages{messages.length ? ` (${messages.length})` : ""}
        </button>
        <p className={`msg-latest ${latest ? `tone-${latest.tone}` : ""}`} role="status">
          {latest ? `${formatTime(latest.time)}  ${latest.text}` : "No messages yet."}
        </p>
        {open && (
          <div className="msg-pop" role="log" aria-label="Message history">
            <div className="msg-pop-head">
              <strong>Message history</strong>
              <button type="button" className="link" onClick={onClear}>Clear</button>
            </div>
            {messages.length === 0 ? (
              <p className="muted">Nothing yet. Uploads, finished loads and engine changes appear here.</p>
            ) : (
              <ul>
                {[...messages].reverse().map((m) => (
                  <li key={m.id} className={`tone-${m.tone}`}>
                    <span className="muted">{formatTime(m.time)}</span> {m.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
      <div className="footer-status">
        {updatedAt && <span className="muted">Updated {formatTime(updatedAt)}</span>}
        <span className={`engine engine-${status}`}>
          <span className="dot" aria-hidden="true" />
          {label}
        </span>
      </div>
    </footer>
  );
}
