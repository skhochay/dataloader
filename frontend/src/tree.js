// The navigation tree. Selecting a leaf decides what the main area shows.
const BASE_TREE = [
  {
    id: "overview",
    label: "Overview",
    title: "Overview",
    description: "Where files are right now, and the latest loads.",
  },
  {
    id: "history",
    label: "Load history",
    children: [
      { id: "jobs:all", label: "All loads", title: "All loads", description: "Every load attempt, newest first." },
      { id: "jobs:loaded", label: "Loaded", title: "Loaded", description: "Loads that completed." },
      { id: "jobs:failed", label: "Failed", title: "Failed", description: "Loads that were rejected. Open one to see why." },
      { id: "jobs:progress", label: "In progress", title: "In progress", description: "Loading now, or queued for a retry." },
    ],
  },
  {
    id: "config",
    label: "Configuration",
    title: "Configuration",
    description: "Timing and loading options. Changes apply on the engine’s next scan; no restart needed.",
  },
];

export function findNode(id, nodes = TREE) {
  for (const node of nodes) {
    if (node.id === id) return node;
    const hit = node.children && findNode(id, node.children);
    if (hit) return hit;
  }
  return null;
}

/** The full tree: Overview, a Directories branch (one leaf per profile), Load history, a Database branch (one leaf per connection), Configuration. */
export function buildTree(connections = [], kinds = [], profiles = [], mappings = []) {
  const labels = Object.fromEntries(kinds.map((k) => [k.kind, k.label]));
  const directories = {
    id: "directories",
    label: "Directories",
    selectable: true, // clicking the branch opens the profiles page, and also expands it
    title: "Directory profiles",
    description: "Each profile is a named set of stage, processing, archive and fail folders. The engine watches every enabled profile.",
    children: [
      {
        id: "prof:new",
        label: "Add profile",
        action: true,
        title: "New profile",
        description: "Name a set of four folders, test them, then save.",
      },
      ...profiles.map((p) => ({
        id: `prof:${p.id}`,
        label: p.name,
        meta: p.enabled ? undefined : "off",
        title: p.name,
        description: "Folders for this profile and the files in them. Edit, test or delete it.",
      })),
    ],
  };
  const database = {
    id: "databases",
    label: "Database",
    selectable: true, // clicking the branch opens the connections page, and also expands it
    title: "Database connections",
    description: "Saved connections to your databases. Add one, test them, or open one to edit.",
    children: [
      {
        id: "db:new",
        label: "Add connection",
        action: true,
        title: "New connection",
        description: "Save a named connection to a database, then test it.",
      },
      ...connections.map((c) => ({
        id: `db:${c.id}`,
        label: c.name,
        meta: labels[c.kind] ?? c.kind,
        title: c.name,
        description: `${labels[c.kind] ?? c.kind} connection. Edit it, test it, or delete it.`,
      })),
    ],
  };
  const mapping = {
    id: "mappings",
    label: "Mapping",
    selectable: true, // clicking the branch opens the mappings page, and also expands it
    title: "Mapping",
    description: "Choose which database each directory profile loads into, and which file names go to which table.",
    children: [
      {
        id: "map:new",
        label: "Add mapping",
        action: true,
        title: "New mapping",
        description: "Pick a directory profile and a database, then say which files go to which table.",
      },
      ...mappings.map((m) => ({
        id: `map:${m.id}`,
        label: m.profile_name,
        meta: m.enabled ? m.connection_name : "off",
        title: `Mapping: ${m.profile_name}`,
        description: `Files of profile ${m.profile_name} load into ${m.connection_name}. Edit the rules, or delete the mapping.`,
      })),
    ],
  };
  const [overview, ...rest] = BASE_TREE; // rest = Load history, Configuration
  const config = rest.pop();
  return [overview, directories, ...rest, database, mapping, config];
}

export const TREE = buildTree();
