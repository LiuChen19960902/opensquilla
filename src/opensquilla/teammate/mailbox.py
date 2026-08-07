"""File-backed per-agent mailbox.

Each team member owns one inbox file (``<team_dir>/inboxes/<name>.json``)
holding a JSON array of ``MailboxMessage`` envelopes. Writes are serialized
in-process with a threading lock and persisted atomically (temp file +
``os.replace``) so a crash never leaves a truncated inbox.

This mirrors the Claude Code ``TeammateMailbox`` design (``getInboxPath`` /
``writeToMailbox`` / ``readMailbox`` / ``markMessageAsReadByIndex``) and gives
teammates a durable, auditable, cross-process channel without any IPC plumbing.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opensquilla.teammate.models import MailboxMessage

# Lock per inbox path — keyed by resolved path string.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Separator used by Mailbox.merge_messages to join backlogged messages into a
# single queued envelope (pi-subagents agent-manager.ts:523-529 style:
# "[QUEUED TEAM MESSAGE]" between coalesced texts).
QUEUED_TEAM_MESSAGE_SEPARATOR = "\n\n[QUEUED TEAM MESSAGE]\n"


class Mailbox:
    """A single agent's inbox file with read/unread tracking."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = _lock_for(path)

    # ── lifecycle ──────────────────────────────────────────────────────
    def ensure(self) -> "Mailbox":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._atomic_write([])
        return self

    def _read_all(self) -> list[MailboxMessage]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        return [MailboxMessage.from_dict(item) for item in raw if isinstance(item, dict)]

    def _atomic_write(self, messages: list[MailboxMessage]) -> None:
        payload = json.dumps(
            [m.to_dict() for m in messages], ensure_ascii=False, indent=2
        )
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    # ── API ────────────────────────────────────────────────────────────
    def send(self, sender: str, body: dict[str, Any]) -> MailboxMessage:
        """Append a typed message to the inbox.

        ``body`` is the inner typed payload; it is JSON-encoded into the
        envelope ``text`` field. Returns the stored envelope.
        """
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        envelope = MailboxMessage(
            from_name=sender,
            text=text,
            timestamp=_now_iso(),
            type=str(body.get("type", "message")) if isinstance(body, dict) else "message",
        )
        with self._lock:
            messages = self._read_all()
            messages.append(envelope)
            self._atomic_write(messages)
        return envelope

    def read_unread(self) -> list[MailboxMessage]:
        """Return and mark-as-read every unread message (atomic)."""
        with self._lock:
            messages = self._read_all()
            unread = [m for m in messages if not m.read]
            if not unread:
                return []
            for m in messages:
                m.read = True
            self._atomic_write(messages)
        return unread

    def peek(self) -> list[MailboxMessage]:
        """Return all messages without mutating read state."""
        with self._lock:
            return self._read_all()

    def count_unread(self) -> int:
        with self._lock:
            return sum(1 for m in self._read_all() if not m.read)

    def requeue(self, messages: list["MailboxMessage"]) -> None:
        """Re-append messages as unread (failed tail recovery, Bug3 fix)."""
        if not messages:
            return
        with self._lock:
            all_msgs = self._read_all()
            for m in messages:
                m.read = False
                all_msgs.append(m)
            self._atomic_write(all_msgs)

    def mark_unread(self, messages: list["MailboxMessage"]) -> int:
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
        with self._lock:
            self._atomic_write([])

    @classmethod
    def open(cls, team_dir: Path, agent_name: str) -> "Mailbox":
        """Open the canonical inbox for ``agent_name`` under ``team_dir``."""
        return cls(team_dir / "inboxes" / f"{agent_name}.json").ensure()
