import { useEffect, useState } from "react";
import { startEngine, stopEngine } from "../api.js";
import { formatDate } from "../format.js";

const START_TIMEOUT_MS = 20000;
const FORCE_AFTER_MS = 10000;

const LABEL = {
  unknown: "Checking",
  running: "Running",
  starting: "Starting",
  stopping: "Stopping",
  stopped: "Stopped",
};

function uptime(startedAt, now) {
  const seconds = Math.max(0, Math.floor((now - new Date(startedAt)) / 1000));
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${seconds} s`;
}

export default function EngineCard({ engine, queue, onChanged, notify }) {
  const [starting, setStarting] = useState(false); // start requested, engine not up yet
  const [busy, setBusy] = useState(false);
  const [stopSince, setStopSince] = useState(null);
  const [now, setNow] = useState(Date.now());

  const running = engine?.running === true;
  const stopping = running && engine.stopping;
  const state = !engine ? "unknown" : stopping ? "stopping" : running ? "running" : starting ? "starting" : "stopped";
  const transitional = state === "starting" || state === "stopping";

  useEffect(() => {
    if (running) setStarting(false);
  }, [running]);

  useEffect(() => {
    if (!starting) return undefined;
    const timer = setTimeout(() => {
      setStarting(false);
      notify?.("The engine didn’t start in time. Check the engine log for the reason.", "bad");
    }, START_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [starting, notify]);

  useEffect(() => {
    setStopSince((since) => (state === "stopping" ? since ?? Date.now() : null));
  }, [state]);

  // Check more often while something is changing.
  useEffect(() => {
    if (!transitional) return undefined;
    const timer = setInterval(() => {
      setNow(Date.now());
      onChanged?.();
    }, 1000);
    return () => clearInterval(timer);
  }, [transitional, onChanged]);

  async function run(action, message) {
    setBusy(true);
    try {
      await action();
      notify?.(message, "info");
    } catch (e) {
      setStarting(false);
      notify?.(e.message, "bad");
    } finally {
      setBusy(false);
      onChanged?.();
    }
  }

  const onStart = () => {
    setStarting(true);
    run(startEngine, "Engine start requested.");
  };
  const onStop = () => run(() => stopEngine(false), "Engine stop requested. It will finish the file it is loading first.");
  const onForce = () => {
    if (window.confirm("Force stop ends the engine immediately. A file being loaded is rolled back and queued again on the next start. Continue?")) {
      run(() => stopEngine(true), "Engine force-stopped.");
    }
  };

  const showForce = state === "stopping" && engine?.can_force && stopSince && now - stopSince > FORCE_AFTER_MS;

  return (
    <section className="engine-card" aria-labelledby="engine-heading">
      <div className="engine-main">
        <span className={`light light-${state}`} role="img" aria-label={`Engine ${LABEL[state].toLowerCase()}`} />
        <div>
          <h2 id="engine-heading">Engine</h2>
          <p className={`engine-state engine-state-${state}`} role="status">{LABEL[state]}</p>
        </div>
      </div>

      <dl className="engine-facts">
        <div>
          <dt>Process ID</dt>
          <dd className="pid">{engine?.pid ?? "–"}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{engine?.started_at ? formatDate(engine.started_at) : "–"}</dd>
        </div>
        <div>
          <dt>Running for</dt>
          <dd>{engine?.started_at ? uptime(engine.started_at, now) : "–"}</dd>
        </div>
        <div>
          <dt>Queued</dt>
          <dd>{queue?.queued ?? "–"}</dd>
        </div>
        <div>
          <dt>Loading now</dt>
          <dd>{queue?.processing ?? "–"}</dd>
        </div>
      </dl>

      <div className="engine-actions">
        <button type="button" className="button button-go" disabled={!engine || running || starting || busy} onClick={onStart}>
          Start
        </button>
        <button type="button" className="button button-halt" disabled={!running || stopping || busy} onClick={onStop}>
          Stop
        </button>
        {showForce && (
          <button type="button" className="link" onClick={onForce}>Force stop</button>
        )}
      </div>

      {running && (
        <div className="engine-workers">
          <p className="muted">
            {engine.worker_target > 0
              ? `Workers: ${engine.workers?.length ?? 0} of ${engine.worker_target}`
              : "Workers: none. The engine loads the files itself, one at a time (set Workers on the Configuration page to load in parallel)."}
          </p>
          {engine.workers?.length > 0 && (
            <ul className="workers" aria-label="Workers">
              {engine.workers.map((w) => (
                <li key={w.slot}>
                  <span className="worker-name">Worker {w.slot}</span>
                  <span className="muted">PID {w.pid}</span>
                  {w.filename ? <span>{w.filename}{w.profile ? ` · ${w.profile}` : ""}</span> : <span className="worker-idle">idle</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {engine?.log_file && <p className="engine-log">Engine output is written to <code>{engine.log_file}</code></p>}
    </section>
  );
}
