import EngineCard from "../components/EngineCard.jsx";
import FlowStrip from "../components/FlowStrip.jsx";
import JobsTable from "../components/JobsTable.jsx";

export default function Overview({ stats, jobs, onSelectJob, onGo, onEngineChanged, notify }) {
  return (
    <>
      <EngineCard engine={stats?.engine} queue={stats?.queue} onChanged={onEngineChanged} notify={notify} />
      <FlowStrip directories={stats?.directories} />
      <section aria-labelledby="recent-heading" className="stack">
        <div className="section-head">
          <h2 id="recent-heading">Recent loads</h2>
          <button type="button" className="link" onClick={() => onGo("jobs:all")}>See all loads</button>
        </div>
        <JobsTable jobs={jobs.slice(0, 8)} onSelect={onSelectJob} />
      </section>
    </>
  );
}
