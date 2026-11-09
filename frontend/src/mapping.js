// Matching and suggestions for the mapping screen. The server (loader/mapping.py) is the authority; this mirrors it so the
// screen can show results while you type.

/** Turns a pattern such as emp*.* into a regular expression: * is any characters, ? is one, [abc] a set. Case is ignored. */
export function globToRegex(pattern) {
  const p = pattern.trim();
  let out = "";
  let i = 0;
  while (i < p.length) {
    const c = p[i++];
    if (c === "*") out += "[\\s\\S]*";
    else if (c === "?") out += "[\\s\\S]";
    else if (c === "[") {
      let j = i;
      if (p[j] === "!") j++;
      if (p[j] === "]") j++;
      while (j < p.length && p[j] !== "]") j++;
      if (j >= p.length) {
        out += "\\[";
      } else {
        let set = p.slice(i, j).replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
        i = j + 1;
        if (set[0] === "!") set = "^" + set.slice(1);
        else if (set[0] === "^") set = "\\" + set;
        out += `[${set}]`;
      }
    } else {
      out += c.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
    }
  }
  try {
    return new RegExp(`^${out}$`, "i");
  } catch {
    return /(?!)/; // a pattern the browser can't read matches nothing; the server decides on save
  }
}

/** Index of the first pattern that fits the file name, or -1. Empty patterns are skipped. */
export function matchRule(patterns, filename) {
  for (let i = 0; i < patterns.length; i++) {
    if (patterns[i] && globToRegex(patterns[i]).test(filename)) return i;
  }
  return -1;
}

export const tableLabel = (t) => (t.schema ? `${t.schema}.${t.table}` : t.table);

/** Tables the typed name refers to: table, schema.table or database.schema.table (the database part is not compared). */
export function findTables(tables, name) {
  const parts = name.trim().split(".");
  const table = parts[parts.length - 1].toLowerCase();
  const schema = parts.length >= 2 ? parts[parts.length - 2].toLowerCase() : null;
  return tables.filter((t) => t.table.toLowerCase() === table && (schema === null || t.schema.toLowerCase() === schema));
}

/** One suggested rule per group of files that have no rule: the letters a name starts with, such as emp for emp001.json. */
export function suggestRules(names, tables) {
  const groups = new Map();
  for (const name of names) {
    const m = name.match(/^[A-Za-z]+/);
    const key = m ? m[0] : name.replace(/\.[^.]*$/, "");
    const hit = groups.get(key.toLowerCase());
    if (hit) hit.count++;
    else groups.set(key.toLowerCase(), { prefix: key, count: 1 });
  }
  return [...groups.values()].slice(0, 12).map(({ prefix, count }) => {
    const match = tables.find((t) => t.table.toLowerCase() === prefix.toLowerCase());
    return { pattern: `${prefix}*.*`, count, table: match ? tableLabel(match) : "" };
  });
}
