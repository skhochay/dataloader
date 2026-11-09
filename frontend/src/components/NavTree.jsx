import { useMemo, useRef, useState } from "react";

function flatten(nodes, expanded, depth = 1, parentId = null, out = []) {
  for (const node of nodes) {
    out.push({ node, depth, parentId });
    if (node.children && expanded.has(node.id)) flatten(node.children, expanded, depth + 1, node.id, out);
  }
  return out;
}

export default function NavTree({ nodes, selectedId, onSelect, counts }) {
  const [expanded, setExpanded] = useState(() => new Set(nodes.filter((n) => n.children).map((n) => n.id)));
  const [focusId, setFocusId] = useState(selectedId);
  const refs = useRef({});
  const visible = useMemo(() => flatten(nodes, expanded), [nodes, expanded]);
  const tabId = visible.some((v) => v.node.id === focusId) ? focusId : visible[0].node.id;

  const toggle = (id) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const moveTo = (id) => {
    setFocusId(id);
    refs.current[id]?.focus();
  };

  const activate = (node) => {
    if (!node.children) return onSelect(node.id);
    if (node.selectable && node.id !== selectedId) {
      // A branch with its own page: first click opens the page and expands; later clicks collapse or expand.
      setExpanded((prev) => new Set(prev).add(node.id));
      return onSelect(node.id);
    }
    return toggle(node.id);
  };

  function onKeyDown(event, index) {
    const { node, parentId } = visible[index];
    const handlers = {
      ArrowDown: () => visible[index + 1] && moveTo(visible[index + 1].node.id),
      ArrowUp: () => visible[index - 1] && moveTo(visible[index - 1].node.id),
      Home: () => moveTo(visible[0].node.id),
      End: () => moveTo(visible[visible.length - 1].node.id),
      ArrowRight: () => {
        if (!node.children) return;
        if (!expanded.has(node.id)) toggle(node.id);
        else moveTo(node.children[0].id);
      },
      ArrowLeft: () => {
        if (node.children && expanded.has(node.id)) toggle(node.id);
        else if (parentId) moveTo(parentId);
      },
      Enter: () => activate(node),
      " ": () => activate(node),
    };
    const handler = handlers[event.key];
    if (handler) {
      event.preventDefault();
      handler();
    }
  }

  return (
    <nav className="nav" aria-label="Objects">
      <ul role="tree" className="tree" aria-label="Objects">
        {visible.map(({ node, depth }, index) => {
          const isGroup = Boolean(node.children);
          const selected = (!isGroup || node.selectable) && node.id === selectedId;
          const count = counts?.[node.id];
          return (
            <li key={node.id} role="none">
              <button
                type="button"
                role="treeitem"
                ref={(el) => { refs.current[node.id] = el; }}
                aria-level={depth}
                aria-expanded={isGroup ? expanded.has(node.id) : undefined}
                aria-selected={isGroup && !node.selectable ? undefined : selected}
                tabIndex={node.id === tabId ? 0 : -1}
                className={`treeitem ${selected ? "treeitem-selected" : ""} ${isGroup ? "treeitem-group" : ""} ${node.action ? "treeitem-action" : ""}`}
                style={{ paddingLeft: `${12 + (depth - 1) * 20}px` }}
                onClick={() => { setFocusId(node.id); activate(node); }}
                onKeyDown={(e) => onKeyDown(e, index)}
              >
                {isGroup ? (
                  <svg className={`chev ${expanded.has(node.id) ? "chev-open" : ""}`} viewBox="0 0 10 10" aria-hidden="true">
                    <path d="M3 1.5L6.5 5L3 8.5" />
                  </svg>
                ) : (
                  <span className={`swatch ${node.tone ? `swatch-${node.tone}` : ""}`} aria-hidden="true" />
                )}
                <span className="treeitem-label" title={node.label}>{node.label}</span>
                {node.meta && <span className="treeitem-meta">{node.meta}</span>}
                {count != null && (
                  <span className={`count ${node.tone === "fail" && count > 0 ? "count-fail" : ""}`}>{count.toLocaleString()}</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
