import { THEME } from "./theme.mjs";
import { memberColor } from "./blocks/teammateBlock.mjs";
import { stripTerminalControls, textWidth } from "./primitives.mjs";
import { destroyRenderable } from "./renderableLifecycle.mjs";

// Team widget — a compact status strip that lives at the top of the transcript
// (or just under the header). It mirrors pi-subagents' live widget: each member
// shows a colored dot + name + state, with token/progress in dim text.
// The widget is intentionally lightweight: one BoxRenderable row, re-rendered
// on demand, hidden when no team is active.

const DOT = {
  running: "●",
  idle: "○",
  spawning: "◐",
  shutting_down: "◑",
  done: "✓",
  error: "✗",
  aborted: "✗",
};

function dotFor(status) {
  return DOT[status] || "●";
}

function colorFor(status, memberName) {
  if (status === "error" || status === "aborted") return THEME.danger;
  if (status === "done") return THEME.ok;
  if (status === "running") return memberColor(memberName);
  if (status === "idle") return THEME.textDim;
  return memberColor(memberName);
}

export function createTeamWidget({ renderer, BoxRenderable, TextRenderable, container }) {
  const box = new BoxRenderable(renderer, {
    id: "team-widget",
    width: "100%",
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 1,
    paddingLeft: 1,
    paddingRight: 1,
    backgroundColor: THEME.bgSurface,
    border: ["bottom"],
    borderColor: THEME.borderMuted || THEME.muted,
    visible: false,
  });
  // Insert at top of container if available, else hide
  try {
    if (container && typeof container.add === "function") {
      // Add as first child — BoxRenderable order matters
      container.add(box, 0);
    }
  } catch {}

  let currentTeamId = null;
  let members = [];

  function render() {
    // Clear previous children
    const children = [...(box.getChildren?.() ?? [])];
    for (const c of children) destroyRenderable(box, c);

    if (!members.length) {
      box.visible = false;
      renderer.requestRender?.();
      return;
    }
    box.visible = true;

    // Title segment
    const title = new TextRenderable(renderer, {
      id: "team-widget-title",
      content: `team ${stripTerminalControls(currentTeamId || "").slice(0, 8)} · `,
      fg: THEME.textDim,
    });
    box.add(title);

    for (const m of members) {
      const dot = dotFor(m.status);
      const col = colorFor(m.status, m.name);
      const label = `${dot} ${stripTerminalControls(m.name)}`;
      const extra = [];
      if (m.tokens) extra.push(`${m.tokens}`);
      if (m.progress) extra.push(m.progress);
      const suffix = extra.length ? ` ${extra.join(" ")}` : "";
      const node = new TextRenderable(renderer, {
        id: `team-widget-${m.name}`,
        content: `${label}${suffix}  `,
        fg: col,
      });
      box.add(node);
    }
    renderer.requestRender?.();
  }

  return {
    box,
    update(teamId, nextMembers) {
      currentTeamId = teamId;
      members = Array.isArray(nextMembers) ? nextMembers : [];
      render();
    },
    clear() {
      members = [];
      render();
    },
    recolor() {
      box.backgroundColor = THEME.bgSurface;
      box.borderColor = THEME.borderMuted || THEME.muted;
      render();
    },
    get visible() { return box.visible; },
  };
}
