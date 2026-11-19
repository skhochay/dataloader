// Navigation-tree icons. One small inline SVG per kind of object, drawn with
// `currentColor` so the colour comes from CSS (`.treeicon-<tone>`) rather than
// being baked into the path. That keeps the palette in styles.css next to the
// rest of the design tokens, and lets a selected or hovered row recolour its
// icon without a second copy of the artwork.
//
// All icons share a 16x16 box and a 1.6 stroke so they line up optically in a
// vertical list. Add one here, then reference it by name from tree.js.

const ICONS = {
  // Overview: a dashboard grid. A gauge was tried first and read as an
  // ambiguous squiggle at 16px -- the arc dominated and the needle vanished.
  overview: (
    <>
      <rect x="2.2" y="2.2" width="5" height="5" rx="1" />
      <rect x="8.8" y="2.2" width="5" height="5" rx="1" />
      <rect x="2.2" y="8.8" width="5" height="5" rx="1" />
      <rect x="8.8" y="8.8" width="5" height="5" rx="1" />
    </>
  ),
  // Directories branch: stacked folders.
  folders: (
    <>
      <path d="M1.5 5.5V4a1 1 0 0 1 1-1h2.3l1 1.2h3.7a1 1 0 0 1 1 1v0.8" />
      <path d="M3.2 6.5h9.3a1 1 0 0 1 1 1v4.3a1 1 0 0 1-1 1H3.2a1 1 0 0 1-1-1V7.5a1 1 0 0 1 1-1Z" />
    </>
  ),
  // One directory profile.
  folder: (
    <path d="M2 12.5V4.2a1 1 0 0 1 1-1h2.6l1.2 1.4H13a1 1 0 0 1 1 1v6.9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1Z" />
  ),
  // Load history branch: a clock.
  history: (
    <>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 4.6V8l2.4 1.6" />
    </>
  ),
  // All loads: a list.
  list: (
    <>
      <path d="M5.5 4.5h8M5.5 8h8M5.5 11.5h8" />
      <circle cx="2.8" cy="4.5" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="2.8" cy="8" r="0.9" fill="currentColor" stroke="none" />
      <circle cx="2.8" cy="11.5" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
  // Loaded: a tick in a circle.
  check: (
    <>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M5.4 8.2 7.2 10l3.4-3.8" />
    </>
  ),
  // Failed: a warning triangle. Distinct in shape as well as colour, so it
  // still reads as "the bad one" without relying on red alone.
  alert: (
    <>
      <path d="M8 2.4 14.2 13H1.8L8 2.4Z" />
      <path d="M8 6.4v3" />
      <circle cx="8" cy="11.2" r="0.8" fill="currentColor" stroke="none" />
    </>
  ),
  // In progress: a refresh arc.
  progress: (
    <>
      <path d="M13.3 8a5.3 5.3 0 1 1-1.9-4.1" />
      <path d="M13.5 2.2v2.6h-2.6" />
    </>
  ),
  // Database branch: the usual stacked cylinder.
  database: (
    <>
      <ellipse cx="8" cy="4.1" rx="5.2" ry="2" />
      <path d="M2.8 4.1v7.8c0 1.1 2.3 2 5.2 2s5.2-0.9 5.2-2V4.1" />
      <path d="M2.8 8c0 1.1 2.3 2 5.2 2s5.2-0.9 5.2-2" />
    </>
  ),
  // One saved connection: a plug.
  plug: (
    <>
      <path d="M6 1.8v3.4M10 1.8v3.4" />
      <path d="M3.9 5.2h8.2v2.1A4.1 4.1 0 0 1 8 11.4a4.1 4.1 0 0 1-4.1-4.1V5.2Z" />
      <path d="M8 11.4v2.8" />
    </>
  ),
  // Mapping branch: two paths joining, i.e. files to tables.
  mapping: (
    <>
      <path d="M2.2 4.4h3.1c1.5 0 2.2 3.6 3.7 3.6h4.8" />
      <path d="M2.2 11.6h3.1c1.5 0 2.2-3.6 3.7-3.6" />
      <path d="M11.6 5.9 13.8 8l-2.2 2.1" />
    </>
  ),
  // One mapping: an arrow to its target.
  arrow: (
    <>
      <path d="M2.4 8h10" />
      <path d="M9.6 5.1 12.5 8l-2.9 2.9" />
    </>
  ),
  // Configuration: sliders.
  sliders: (
    <>
      <path d="M2.4 4.6h11.2M2.4 11.4h11.2" />
      <circle cx="6" cy="4.6" r="1.7" fill="var(--panel)" />
      <circle cx="10.4" cy="11.4" r="1.7" fill="var(--panel)" />
    </>
  ),
  // The "Add ..." rows.
  plus: (
    <>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 5.3v5.4M5.3 8h5.4" />
    </>
  ),
};

/**
 * One tree icon. `name` picks the artwork and `tone` picks the colour class;
 * an unknown name renders nothing so a new tree node cannot crash the sidebar.
 */
export default function TreeIcon({ name, tone }) {
  const art = ICONS[name];
  if (!art) return null;
  return (
    <svg
      className={`treeicon ${tone ? `treeicon-${tone}` : ""}`}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
    >
      {art}
    </svg>
  );
}
