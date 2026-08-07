import { THEME } from "../theme.mjs";
import { stripTerminalControls } from "../primitives.mjs";
import { destroyRenderable } from "../renderableLifecycle.mjs";

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
  const COMPLETED_PREVIEW_ROWS = 8;
  let body = null;
  let label = null;
  let labelDot = null;
  let content = null;
  let railColor = THEME.promptAccent;
  let memberName = "";
  let rawText = "";
  let done = false;
  let expanded = false;
  let hiddenLineCount = 0;
  const nodes = []; // body line nodes
  let summaryNode = null;

  function scheduleRender() {
    // Immediate render for correctness; coalescing is handled by the caller's
    // 32ms flush in surface.py. Keeping this synchronous guarantees test
    // determinism and avoids setTimeout drift in bun test environments.
    doRender();
    renderer.requestRender?.();
  }

  function allRows() {
    const safe = stripTerminalControls(rawText);
    if (!safe) return [];
    // Preserve interior blank lines, drop single trailing empty caused by split
    const logical = safe.split("\n");
    while (logical.length > 1 && logical.at(-1) === "") logical.pop();
    return logical;
  }

  function doRender() {
    if (!content) return;
    const rows = allRows();
    const collapsed = done && !expanded && rows.length > COMPLETED_PREVIEW_ROWS;
    const visible = collapsed ? rows.slice(0, COMPLETED_PREVIEW_ROWS) : rows;
    hiddenLineCount = collapsed ? rows.length - visible.length : 0;

    // Reconcile nodes to visible count
    while (nodes.length > visible.length) {
      const n = nodes.pop();
      destroyRenderable(content, n);
    }
    while (nodes.length < visible.length) {
      const idx = nodes.length;
      const n = new TextRenderable(renderer, {
        id: `${idPrefix}-l${idx}`, content: " ", fg: THEME.text,
      });
      content.add(n);
      nodes.push(n);
    }
    visible.forEach((line, i) => {
      nodes[i].content = line || " ";
      nodes[i].fg = THEME.text;
    });

    // Summary row for collapsed state
    if (hiddenLineCount > 0) {
      const text = `  … ${hiddenLineCount} more lines · expand`;
      if (!summaryNode) {
        summaryNode = new TextRenderable(renderer, {
          id: `${idPrefix}-summary`, content: text, fg: THEME.textDim,
        });
        content.add(summaryNode);
      } else {
        summaryNode.content = text;
      }
    } else if (summaryNode) {
      destroyRenderable(content, summaryNode);
      summaryNode = null;
    }
  }

  function ensureShell(name) {
    if (body) return;
    memberName = name;
    railColor = name ? memberColor(name) : THEME.promptAccent;
    body = new BoxRenderable(renderer, {
      id: `${idPrefix}-body`, width: "100%", flexDirection: "row",
      border: ["left"], borderColor: railColor,
      backgroundColor: THEME.promptSurface,
      paddingLeft: 1, paddingRight: 1, flexShrink: 0,
    });
    box.add(body);
    if (name) {
      // Status dot + pill-style name: dot carries member hue, name keeps hue for scannability
      labelDot = new TextRenderable(renderer, {
        id: `${idPrefix}-dot`, content: "● ", fg: railColor,
        flexShrink: 0, wrapMode: "none",
      });
      body.add(labelDot);
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
  }

  return {
    get rawText() { return rawText; },
    get isExpanded() { return expanded; },
    get hiddenLineCount() { return hiddenLineCount; },
    begin(meta) {
      const raw = String(meta?.text ?? "");
      const colon = raw.indexOf(":");
      const name = (colon > 0 ? raw.slice(0, colon) : String(meta?.from ?? "")).trim();
      const rest = colon > 0 ? raw.slice(colon + 1).trim() : raw;
      ensureShell(name);
      rawText = rest;
      done = false;
      expanded = false;
      doRender();
      renderer.requestRender?.();
    },
    append(delta) {
      if (!body) ensureShell("");
      rawText += String(delta ?? "");
      scheduleRender();
    },
    update(meta) {
      // Future: token/progress updates can be rendered as a dim suffix row
      // For now, treat text updates as append replacement if provided
      if (meta && typeof meta.text === "string" && meta.text !== rawText) {
        rawText = String(meta.text ?? rawText);
        const name = String(meta.from ?? memberName).trim();
        if (name && name !== memberName) {
          memberName = name;
          railColor = memberColor(name);
          if (label) label.fg = railColor;
          if (labelDot) labelDot.fg = railColor;
          if (body) body.borderColor = railColor;
        }
        doRender();
        renderer.requestRender?.();
      }
    },
    end() {
      done = true;
      doRender();
      renderer.requestRender?.();
    },
    toggleExpanded() {
      if (!done || hiddenLineCount === 0) return false;
      expanded = !expanded;
      doRender();
      renderer.requestRender?.();
      return expanded;
    },
    recolor() {
      for (const n of nodes) n.fg = THEME.text;
      if (summaryNode) summaryNode.fg = THEME.textDim;
      if (label) label.fg = railColor;
      if (labelDot) labelDot.fg = railColor;
      if (body) {
        body.borderColor = railColor;
        body.backgroundColor = THEME.promptSurface;
      }
      if (content) content.backgroundColor = THEME.promptSurface;
    },
  };
}
