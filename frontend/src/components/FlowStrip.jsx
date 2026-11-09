function Lane({ tone, title, count, note }) {
  return (
    <div className={`lane lane-${tone}`}>
      <p className="lane-count">{count}</p>
      <div>
        <h2>{title}</h2>
        <p>{note}</p>
      </div>
    </div>
  );
}

export default function FlowStrip({ directories }) {
  const d = directories ?? { stage: "–", archive: "–", fail: "–" };
  return (
    <section className="flow" aria-label="Files in each directory">
      <Lane tone="stage" title="Stage" count={d.stage} note="Waiting to load" />
      <svg className="flow-link" viewBox="0 0 64 100" preserveAspectRatio="none" aria-hidden="true">
        <path d="M0 50H30V25H64M30 50V75H64" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flow-out">
        <Lane tone="archive" title="Archive" count={d.archive} note="Loaded successfully" />
        <Lane tone="fail" title="Fail" count={d.fail} note="Needs attention" />
      </div>
    </section>
  );
}
