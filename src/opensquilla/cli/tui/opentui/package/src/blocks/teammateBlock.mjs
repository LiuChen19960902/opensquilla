import { THEME } from "../theme.mjs";
import { stripTerminalControls } from "../primitives.mjs";

// A teammate message is a real chat message from a team member (Claude Code
// shared-transcript behavior): a colored member-name label + white body, so
// the lead sees the team conversation at a glance. Unlike the `you` prompt it
// is NOT the user's input — it is inbound team traffic, attributed with a
// stable per-member color. It stays compact (one row per line) and lives
// outside the assistant turn cards, exactly like prompts.
//
// The incoming text is shaped `Name: body` (Python side), so the label is the
// part before the first colon and the body is the rest. A message without a
// colon renders as plain white text with a neutral label.
const MEMBER_COLORS = [
  "#E05A5A", // red
  "#5AB45A", // green
  "#5A8FE0", // blue
  "#D9A53B", // amber
  "#9B6FE0", // purple
  "#3BC0C8", // cyan
  "#E08A3B", // orange
  "#D05A9B", // pink
];

export function memberColor(name) {
  let h = 0;
  const s = String(name || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return MEMBER_COLORS[h % MEMBER_COLORS.length];
}

export function createTeammateBlock(ctx) {
  const { renderer, BoxRenderable, TextRenderable, box, idPrefix } = ctx;
  let body = null;
  let label = null;
  let content = null;
  let railColor = THEME.promptAccent;
  const nodes = []; // every body text node, so a live /theme can recolor them
  return {
    begin(meta) {
      const raw = String(meta?.text ?? "");
      const colon = raw.indexOf(":");
      const name = (colon > 0 ? raw.slice(0, colon) : "").trim();
      const rest = colon > 0 ? raw.slice(colon + 1).trim() : raw;
      railColor = name ? memberColor(name) : THEME.promptAccent;
      body = new BoxRenderable(renderer, {
        id: `${idPrefix}-body`, width: "100%", flexDirection: "row",
        border: ["left"], borderColor: railColor,
        backgroundColor: THEME.promptSurface,
        paddingLeft: 1, paddingRight: 1, flexShrink: 0,
      });
      box.add(body);
      if (name) {
        label = new TextRenderable(renderer, {
          id: `${idPrefix}-label`, content: name, fg: railColor,
          flexShrink: 0, wrapMode: "none",
        });
        body.add(label);
        const gap = new TextRenderable(renderer, {
          id: `${idPrefix}-gap`, content: "  ", fg: THEME.textDim,
          flexShrink: 0, wrapMode: "none",
        });
        body.add(gap);
      }
      content = new BoxRenderable(renderer, {
        id: `${idPrefix}-content`, flexDirection: "column", flexGrow: 1, flexShrink: 1,
        backgroundColor: THEME.promptSurface,
      });
      body.add(content);
      stripTerminalControls(rest).split("\n").forEach((line, i) => {
        const n = new TextRenderable(renderer, {
          id: `${idPrefix}-l${i}`, content: line || " ", fg: THEME.text,
        });
        content.add(n);
        nodes.push(n);
      });
      renderer.requestRender?.();
    },
    append() {}, update() {}, end() {},
    // Live /theme switch: body text follows the theme; the member rail/label
    // keep their stable per-member hue.
    recolor() {
      for (const n of nodes) n.fg = THEME.text;
      if (label) label.fg = railColor;
      if (body) {
        body.borderColor = railColor;
        body.backgroundColor = THEME.promptSurface;
      }
      if (content) content.backgroundColor = THEME.promptSurface;
    },
  };
}
