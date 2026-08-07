// Three-state detail mode regression for thinking/reasoning blocks.
//
// Ctrl+O now cycles detailed → collapsed → hidden → detailed. The hidden
// (fully folded) state tears the block down to zero rows — not even the
// header marker remains — while rawText is retained so unhiding rebuilds the
// complete payload, including deltas that stream in while hidden.
//
// Must run under bun: @opentui/core/testing needs bun FFI.
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable, MarkdownRenderable } from "@opentui/core";

import { createThinkingBlock } from "./blocks/thinkingBlock.mjs";
import { createReasoningBlock } from "./blocks/reasoningBlock.mjs";
import { createTurnView } from "./turnView.mjs";

const WIDTH = 60;
const HEIGHT = 12;

async function harness() {
  const setup = await createTestRenderer({ width: WIDTH, height: HEIGHT });
  const { renderer, renderOnce, captureSpans } = setup;
  const box = new BoxRenderable(renderer, {
    id: "turn",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(box);
  const ctx = {
    renderer,
    BoxRenderable,
    TextRenderable,
    MarkdownRenderable,
    syntaxStyle: undefined,
    box,
    idPrefix: "blk",
  };
  return { renderer, renderOnce, captureSpans, ctx };
}

function flatText(frame) {
  return frame.lines
    .map((line) => line.spans.map((s) => s.text).join(""))
    .join("\n");
}

test("reasoning block: setDetailsMode cycles detailed → collapsed → hidden → detailed", async () => {
  const { renderer, renderOnce, captureSpans, ctx } = await harness();
  try {
    const block = createReasoningBlock(ctx);
    block.begin({});
    block.append("alpha\nbeta\ngamma\ndelta");
    block.end();
    await renderOnce();

    // detailed: full payload + collapse hint
    block.setDetailsMode("detailed");
    await renderOnce();
    let text = flatText(captureSpans());
    expect(text).toContain("alpha");
    expect(text).toContain("delta");
    expect(text).toContain("collapse details");

    // collapsed: header marker retained. A short payload (4 rows < the 8-row
    // completed preview cap) stays fully visible — existing behavior.
    block.setDetailsMode("collapsed");
    await renderOnce();
    text = flatText(captureSpans());
    expect(text).toContain("Thought for");
    expect(text).toContain("alpha");
    expect(text).toContain("delta");

    // hidden: zero rows — no header, no content, no hint
    block.setDetailsMode("hidden");
    await renderOnce();
    text = flatText(captureSpans());
    expect(text).not.toContain("Thought for");
    expect(text).not.toContain("alpha");
    expect(text).not.toContain("✻");
    expect(block.hiddenLineCount).toBe(0);

    // back to detailed rebuilds the full retained payload
    block.setDetailsMode("detailed");
    await renderOnce();
    text = flatText(captureSpans());
    expect(text).toContain("alpha");
    expect(text).toContain("delta");
    expect(block.rawText).toBe("alpha\nbeta\ngamma\ndelta");
  } finally {
    renderer.destroy?.();
  }
});

test("reasoning block: deltas streamed while hidden are retained and appear on unhide", async () => {
  const { renderer, renderOnce, captureSpans, ctx } = await harness();
  try {
    const block = createReasoningBlock(ctx);
    block.begin({});
    block.append("first visible");
    await renderOnce();
    expect(flatText(captureSpans())).toContain("first visible");

    block.setDetailsMode("hidden");
    await renderOnce();
    expect(flatText(captureSpans())).not.toContain("first visible");

    // Stream continues while hidden — nothing renders…
    block.append("streamed in the dark");
    await renderOnce();
    expect(flatText(captureSpans())).not.toContain("streamed in the dark");

    // …but rawText retains it, and unhide shows the complete trace.
    expect(block.rawText).toBe("first visiblestreamed in the dark");
    block.setDetailsMode("detailed");
    await renderOnce();
    const text = flatText(captureSpans());
    expect(text).toContain("first visible");
    expect(text).toContain("streamed in the dark");
  } finally {
    renderer.destroy?.();
  }
});

test("thinking block: hidden removes narration rows and unhide restores them", async () => {
  const { renderer, renderOnce, captureSpans, ctx } = await harness();
  try {
    const block = createThinkingBlock(ctx);
    block.begin({});
    block.append("narration line one\nnarration line two");
    block.end();
    await renderOnce();
    expect(flatText(captureSpans())).toContain("narration line one");

    block.setDetailsMode("hidden");
    await renderOnce();
    const hidden = flatText(captureSpans());
    expect(hidden).not.toContain("narration line one");
    expect(hidden).not.toContain("✻");

    block.setDetailsMode("collapsed");
    await renderOnce();
    const restored = flatText(captureSpans());
    expect(restored).toContain("narration line one");
    expect(block.rawText).toBe("narration line one\nnarration line two");
  } finally {
    renderer.destroy?.();
  }
});

test("turn-level cycleDetailsMode: detailed → collapsed → hidden → detailed, thinking hidden, tool stays collapsed", async () => {
  const setup = await createTestRenderer({ width: 76, height: 42 });
  const { renderer, renderOnce, captureSpans } = setup;
  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation", position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
    flexDirection: "column",
  });
  renderer.root.add(conversationBox);
  const turn = createTurnView(
    { renderer, BoxRenderable, TextRenderable, MarkdownRenderable, syntaxStyle: undefined, conversationBox },
    "three-state",
  );
  const reasoning = Array.from({ length: 12 }, (_, index) => `reason ${index + 1}`).join("\n");
  const toolOut = "tool output";
  turn.begin("r", "reasoning", {});
  turn.append("r", reasoning);
  turn.end("r");
  turn.begin("t", "tool", { name: "probe", args: "value" });
  turn.append("t", toolOut);
  turn.update("t", { status: "ok" });
  turn.end("t");
  turn.finish();
  try {
    await renderOnce();

    // Default mode is collapsed: the completed reasoning keeps a bounded tail
    // (last 8 rows visible), the older rows are folded behind a disclosure.
    expect(turn.detailsMode).toBe("collapsed");
    let text = flatText(captureSpans());
    expect(text).toContain("reason 5");
    expect(text).toContain("4 earlier · Ctrl+O details");
    expect(text).not.toMatch(/\breason 1\b/);
    expect(turn.blockState("r").isExpanded).toBe(false);

    // 1: detailed — full reasoning + tool detail.
    expect(turn.cycleDetailsMode()).toBe("detailed");
    await renderOnce();
    text = flatText(captureSpans());
    expect(turn.detailsMode).toBe("detailed");
    expect(text).toContain("reason 1");
    expect(text).toContain(toolOut);
    expect(turn.blockState("r").isExpanded).toBe(true);

    // 2: collapsed — the bounded preview returns.
    expect(turn.cycleDetailsMode()).toBe("collapsed");
    await renderOnce();
    text = flatText(captureSpans());
    expect(turn.detailsMode).toBe("collapsed");

    // 3: hidden — reasoning fully folded (zero rows), tool stays collapsed.
    expect(turn.cycleDetailsMode()).toBe("hidden");
    await renderOnce();
    text = flatText(captureSpans());
    expect(turn.detailsMode).toBe("hidden");
    expect(text).not.toContain("reason 1");
    expect(text).not.toContain("reason 12");
    expect(text).not.toContain("Thought for");
    expect(text).not.toContain("✻");
    // tool block is NOT hidden: collapsed behavior keeps its one-line summary
    expect(turn.blockState("r").isExpanded).toBe(false);
    expect(turn.blockState("r").rawText).toBe(reasoning);

    // 4: back to detailed — rawText rebuild restores the full payload.
    expect(turn.cycleDetailsMode()).toBe("detailed");
    await renderOnce();
    text = flatText(captureSpans());
    expect(text).toContain("reason 1");
    expect(text).toContain("reason 12");

    // Shift+Ctrl+O walks backwards: detailed → hidden.
    expect(turn.cycleDetailsModeBack()).toBe("hidden");
    await renderOnce();
    expect(flatText(captureSpans())).not.toContain("reason 1");
  } finally {
    renderer.destroy?.();
  }
});

test("flow-level detailsMode is inherited by future turns and updates existing turns", async () => {
  const { renderer, renderOnce, captureSpans } = await createTestRenderer({ width: 76, height: 42 });
  try {
    let seq = 0;
    const flow = await import("./turnView.mjs").then((m) =>
      m.createTurnFlow((id) => {
        const conversationBox = new BoxRenderable(renderer, {
          id: `conversation-${seq}`, position: "absolute", left: 0, top: 0, right: 0, bottom: 0,
          flexDirection: "column",
        });
        renderer.root.add(conversationBox);
        return createTurnView(
          { renderer, BoxRenderable, TextRenderable, MarkdownRenderable, syntaxStyle: undefined, conversationBox },
          id ?? `flow-${seq++}`,
        );
      })
    );
    const first = flow.ensure("t1");
    expect(first.detailsMode).toBe("collapsed"); // default inherited on create

    expect(flow.cycleDetailsMode()).toBe("detailed");
    expect(first.detailsMode).toBe("detailed");

    flow.endTurn();
    const second = flow.ensure("t2");
    expect(second.detailsMode).toBe("detailed"); // future turn inherits

    // detailed → collapsed (bounded preview), inherited by both turns.
    expect(flow.cycleDetailsMode()).toBe("collapsed");
    expect(first.detailsMode).toBe("collapsed");
    expect(second.detailsMode).toBe("collapsed");

    // collapsed → hidden (fully folded).
    expect(flow.cycleDetailsMode()).toBe("hidden");
    expect(first.detailsMode).toBe("hidden");
    expect(second.detailsMode).toBe("hidden");
  } finally {
    renderer.destroy?.();
  }
});
