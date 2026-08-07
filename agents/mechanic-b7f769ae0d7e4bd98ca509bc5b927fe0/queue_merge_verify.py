"""Minimal behavior check for Mailbox.merge_messages.

Run AFTER applying queue_merge_fix_apply.py:

    python queue_merge_verify.py            # against real src/
    python queue_merge_verify.py --copy DIR # against a patched copy

Checks:
  1. 3 backlogged messages -> merged into 1 unread envelope whose text
     contains all 3 originals joined by the [QUEUED TEAM MESSAGE] separator;
     type stays "message", from is the first message's sender, position kept.
  2. Single-message batch is NOT merged: restored unread in place, no
     separator, text unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

COPY = None
if "--copy" in sys.argv:
    COPY = Path(sys.argv[sys.argv.index("--copy") + 1])
    sys.path.insert(0, str(COPY))
else:
    sys.path.insert(0, "/workpace/opensquilla/src")

from opensquilla.teammate.mailbox import (  # noqa: E402
    QUEUED_TEAM_MESSAGE_SEPARATOR,
    Mailbox,
)
from opensquilla.teammate.models import MailboxMessage  # noqa: E402

EXPECTED = (
    "first" + QUEUED_TEAM_MESSAGE_SEPARATOR
    + "second" + QUEUED_TEAM_MESSAGE_SEPARATOR
    + "third"
)


def _reset(mb: Mailbox, msgs: list[MailboxMessage]) -> None:
    mb._atomic_write(msgs)


def _check(cond: bool, label: str) -> None:
    if not cond:
        print(f"[FAIL] {label}")
        sys.exit(1)
    print(f"[OK] {label}")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mb = Mailbox(Path(td) / "inbox.json").ensure()

        # ── scenario 1: 3 backlogged messages ─────────────────────────
        three = [
            MailboxMessage(from_name="lead", text="first", timestamp="t1"),
            MailboxMessage(from_name="lead", text="second", timestamp="t2"),
            MailboxMessage(from_name="lead", text="third", timestamp="t3"),
        ]
        _reset(mb, three)
        # poll: read_unread marks everything read, then run_turn returns
        # TURN_QUEUE_FULL with processed_count == 0
        batch = mb.read_unread()
        _check(len(batch) == 3, "read_unread returns 3 backlogged messages")
        merged = mb.merge_messages(batch)
        remaining = mb.peek()
        _check(merged is not None, "merge_messages returns an envelope")
        _check(len(remaining) == 1, "inbox has exactly 1 message after merge")
        _check(remaining[0].text == EXPECTED, "merged text preserves all 3 originals in order")
        _check(remaining[0].read is False, "merged message is unread (next poll retries)")
        _check(remaining[0].type == "message", "merged type stays 'message'")
        _check(remaining[0].from_name == "lead", "merged from is first message's sender")
        _check(remaining[0].timestamp == "t1", "merged timestamp is first message's timestamp")
        _check(remaining[0] is not three[0], "merge creates a new envelope (no aliasing)")

        # ── scenario 2: single message must NOT be merged ─────────────
        single = [MailboxMessage(from_name="lead", text="only", timestamp="t9")]
        _reset(mb, single)
        got = mb.read_unread()
        result = mb.merge_messages(got)
        after = mb.peek()
        _check(result is got[0], "single-message batch returns the same envelope")
        _check(len(after) == 1, "single-message batch keeps inbox length 1")
        _check(after[0].text == "only", "single-message text unchanged (no merge)")
        _check(QUEUED_TEAM_MESSAGE_SEPARATOR not in after[0].text, "no separator added for single message")
        _check(after[0].read is False, "single message restored to unread")

        # ── scenario 3: empty batch is a no-op ────────────────────────
        _reset(mb, three)
        nothing = mb.merge_messages([])
        _check(nothing is None, "empty batch returns None")
        _check(len(mb.peek()) == 3, "empty batch leaves inbox untouched")

    print("All merge_messages behavior checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
