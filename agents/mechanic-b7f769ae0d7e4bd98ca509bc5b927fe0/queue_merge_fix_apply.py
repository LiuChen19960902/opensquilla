"""Apply the "queue full -> merge backlog" upgrade to opensquilla.teammate.

Usage (from repo root or anywhere; paths are absolute):
  python queue_merge_fix_apply.py --check   # verify all replacements match (no writes)
  python queue_merge_fix_apply.py           # apply the patch

Two files change:
  1. src/opensquilla/teammate/mailbox.py
       - module-level QUEUED_TEAM_MESSAGE_SEPARATOR constant
       - new Mailbox.merge_messages() (pi-subagents style coalescing)
  2. src/opensquilla/teammate/runtime.py
       - TURN_QUEUE_FULL branch: merge_messages() instead of mark_unread()

llm_executor.py is untouched (TURN_QUEUE_FULL sentinel stays as-is).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path("/workpace/opensquilla")

SEP = "\\n\\n[QUEUED TEAM MESSAGE]\\n"

# --- mailbox.py edits -----------------------------------------------------
MAILBOX_CONSTANT_OLD = '''def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Mailbox:
'''

MAILBOX_CONSTANT_NEW = '''def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Separator used by Mailbox.merge_messages to join backlogged messages into a
# single queued envelope (pi-subagents agent-manager.ts:523-529 style:
# "[QUEUED TEAM MESSAGE]" between coalesced texts).
QUEUED_TEAM_MESSAGE_SEPARATOR = "\\n\\n[QUEUED TEAM MESSAGE]\\n"


class Mailbox:
'''

MAILBOX_MERGE_OLD = '''    def mark_unread(self, messages: list["MailboxMessage"]) -> int:
        """In-place restore of specific messages to unread (no copy).

        Matches by (from_name, text, timestamp); returns how many restored.
        """
        if not messages:
            return 0
        keys = {(m.from_name, m.text, m.timestamp) for m in messages}
        restored = 0
        with self._lock:
            all_msgs = self._read_all()
            for m in all_msgs:
                if (m.from_name, m.text, m.timestamp) in keys and m.read:
                    m.read = False
                    restored += 1
            if restored:
                self._atomic_write(all_msgs)
        return restored

    def clear(self) -> None:
'''

MAILBOX_MERGE_NEW = '''    def mark_unread(self, messages: list["MailboxMessage"]) -> int:
        """In-place restore of specific messages to unread (no copy).

        Matches by (from_name, text, timestamp); returns how many restored.
        """
        if not messages:
            return 0
        keys = {(m.from_name, m.text, m.timestamp) for m in messages}
        restored = 0
        with self._lock:
            all_msgs = self._read_all()
            for m in all_msgs:
                if (m.from_name, m.text, m.timestamp) in keys and m.read:
                    m.read = False
                    restored += 1
            if restored:
                self._atomic_write(all_msgs)
        return restored

    def merge_messages(
        self, messages: list["MailboxMessage"]
    ) -> "MailboxMessage | None":
        """Coalesce a backlog batch into one unread queued message.

        pi-subagents agent-manager style: each session keeps a single pending
        turn, and messages that arrive while a turn is in flight are merged
        into that same prompt, separated by ``[QUEUED TEAM MESSAGE]``.

        The batch (identified by (from_name, text, timestamp)) is removed from
        the inbox in-place and replaced by one merged envelope at the position
        of the first batch message; the merged message is unread so the next
        poll processes it as a single turn. Order is preserved and no text is
        dropped.

        A single-message batch is *not* merged (pointless): it is simply
        restored to unread in place, matching the old mark_unread semantics.

        Returns the envelope now representing the batch (the merged message,
        or the single message when no merge happened), or None when the batch
        was empty or no longer present in the inbox.
        """
        if not messages:
            return None
        if len(messages) == 1:
            self.mark_unread(messages)
            return messages[0]
        merged = MailboxMessage(
            from_name=messages[0].from_name,
            text=QUEUED_TEAM_MESSAGE_SEPARATOR.join(m.text for m in messages),
            timestamp=messages[0].timestamp,
            type="message",
            read=False,
        )
        keys = {(m.from_name, m.text, m.timestamp) for m in messages}
        with self._lock:
            all_msgs = self._read_all()
            kept: list[MailboxMessage] = []
            first_idx: int | None = None
            for i, m in enumerate(all_msgs):
                if (m.from_name, m.text, m.timestamp) in keys:
                    if first_idx is None:
                        first_idx = i
                else:
                    kept.append(m)
            if first_idx is None:
                # Batch not found (already processed/removed) — nothing to do.
                return None
            kept.insert(first_idx, merged)
            self._atomic_write(kept)
        return merged

    def clear(self) -> None:
'''

# --- runtime.py edits ------------------------------------------------------
RUNTIME_OLD = '''                    if reply is TURN_QUEUE_FULL:
                        # Backpressure: session pending queue full. Restore unread in place
                        # (no copy) for this and all later messages, stop this batch. The
                        # teammate stays alive; next poll retries when slots free up.
                        try:
                            mailbox.mark_unread(messages[processed_count:])
                        except Exception:
                            pass
                        break
'''

RUNTIME_NEW = '''                    if reply is TURN_QUEUE_FULL:
                        # Backpressure: session pending queue full. Coalesce the failed
                        # and trailing messages into one queued envelope (pi-subagents
                        # style: one turn per session, [QUEUED TEAM MESSAGE] separated).
                        # The teammate stays alive; the next poll retries the merged
                        # message as a single turn.
                        try:
                            mailbox.merge_messages(messages[processed_count:])
                        except Exception:
                            pass
                        break
'''

FILES = {
    "src/opensquilla/teammate/mailbox.py": [
        (MAILBOX_CONSTANT_OLD, MAILBOX_CONSTANT_NEW),
        (MAILBOX_MERGE_OLD, MAILBOX_MERGE_NEW),
    ],
    "src/opensquilla/teammate/runtime.py": [
        (RUNTIME_OLD, RUNTIME_NEW),
    ],
}


def main() -> int:
    check_only = "--check" in sys.argv
    ok = True
    for rel, edits in FILES.items():
        path = REPO / rel
        src = path.read_text(encoding="utf-8")
        for old, new in edits:
            n = src.count(old)
            if n != 1:
                print(f"[FAIL] {rel}: expected 1 occurrence of block, found {n}")
                ok = False
                continue
            if not check_only:
                src = src.replace(old, new)
        if ok and not check_only:
            path.write_text(src, encoding="utf-8")
        print(f"[{'OK' if ok else 'FAIL'}] {rel} ({'check only' if check_only else 'applied'})")
    if not ok:
        print("One or more replacements did not match — no writes performed.")
        return 1
    if check_only:
        print("All replacements match — safe to apply with no --check.")
    else:
        print("Patch applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
