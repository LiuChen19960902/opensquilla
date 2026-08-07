#!/usr/bin/env python3
"""Apply the queue-full backpressure fix to three teammate files.

Usage:
    python queue_full_fix_apply.py --check   # verify all replacements match, write nothing
    python queue_full_fix_apply.py           # apply the fix in place

Safe: every replacement is exact-match + assert (count == 1); the script
aborts with a clear message if the file content does not match expectations.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path("/workpace/opensquilla")
FILES = {
    "src/opensquilla/teammate/mailbox.py": (
        # (old, new) — insert mark_unread right after requeue
        (
            '''    def requeue(self, messages: list["MailboxMessage"]) -> None:
        """Re-append messages as unread (failed tail recovery, Bug3 fix)."""
        if not messages:
            return
        with self._lock:
            all_msgs = self._read_all()
            for m in messages:
                m.read = False
                all_msgs.append(m)
            self._atomic_write(all_msgs)
''',
            '''    def requeue(self, messages: list["MailboxMessage"]) -> None:
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
''',
        ),
    ),
    "src/opensquilla/teammate/llm_executor.py": (
        # 1) import TaskQueueFullError (after gateway.routing import)
        (
            '''from opensquilla.engine.teammate import TeammateHandle, TeammateManager
from opensquilla.gateway.routing import build_subagent_route_envelope
from opensquilla.session.models import AgentTaskStatus
''',
            '''from opensquilla.engine.teammate import TeammateHandle, TeammateManager
from opensquilla.gateway.routing import build_subagent_route_envelope
from opensquilla.gateway.task_runtime import TaskQueueFullError
from opensquilla.session.models import AgentTaskStatus
''',
        ),
        # 2) module-level sentinel after the logger
        (
            '''log = logging.getLogger("opensquilla.teammate.llm_executor")

TERMINAL_OK = frozenset({AgentTaskStatus.SUCCEEDED})
''',
            '''log = logging.getLogger("opensquilla.teammate.llm_executor")

# Sentinel returned by run_turn when the task queue is full (backpressure).
TURN_QUEUE_FULL = object()

TERMINAL_OK = frozenset({AgentTaskStatus.SUCCEEDED})
''',
        ),
        # 3) wrap the enqueue call in try/except TaskQueueFullError
        (
            '''        task = await self.task_runtime.enqueue(
            envelope,
            text,
            mode="followup",
            run_kind="teammate",
        )
        self._pending.setdefault(handle.run_id, []).append(
''',
            '''        try:
            task = await self.task_runtime.enqueue(
                envelope,
                text,
                mode="followup",
                run_kind="teammate",
            )
        except TaskQueueFullError:
            log.warning("teammate.queue_full_backoff session=%s", handle.session_key)
            return TURN_QUEUE_FULL
        self._pending.setdefault(handle.run_id, []).append(
''',
        ),
    ),
    "src/opensquilla/teammate/runtime.py": (
        # 1) import TURN_QUEUE_FULL (after mailbox import)
        (
            '''from opensquilla.engine.teammate import TeammateHandle, TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
''',
            '''from opensquilla.engine.teammate import TeammateHandle, TeammateManager, team_dir_of
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
''',
        ),
        # 2) check sentinel after run_turn in the poll loop
        (
            '''                    reply = await self.executor.run_turn(self.manager, handle, message)
                    if reply:
                        replies.append(f"{handle.name}: {reply}")
''',
            '''                    reply = await self.executor.run_turn(self.manager, handle, message)
                    if reply is TURN_QUEUE_FULL:
                        # Backpressure: session pending queue full. Restore unread in place
                        # (no copy) for this and all later messages, stop this batch. The
                        # teammate stays alive; next poll retries when slots free up.
                        try:
                            mailbox.mark_unread(messages[processed_count:])
                        except Exception:
                            pass
                        break
                    if reply:
                        replies.append(f"{handle.name}: {reply}")
''',
        ),
    ),
}


def main() -> int:
    check_only = "--check" in sys.argv
    ok = True
    for rel, edits in FILES.items():
        path = REPO / rel
        src = path.read_text(encoding="utf-8")
        for i, (old, new) in enumerate(edits, 1):
            n = src.count(old)
            if n != 1:
                print(f"[FAIL] {rel} edit#{i}: expected 1 occurrence, found {n}")
                ok = False
                continue
            if not check_only:
                src = src.replace(old, new)
        if ok and not check_only:
            path.write_text(src, encoding="utf-8")
        print(f"[{'OK' if ok else 'FAIL'}] {rel} ({'check only' if check_only else 'written'})")
    if not ok:
        return 1
    if check_only:
        print("All replacements match — safe to apply with no --check.")
    else:
        print("Fix applied. Verify with:")
        print("  cd /workpace/opensquilla && .venv/bin/python -c \"import opensquilla.teammate.runtime, opensquilla.teammate.llm_executor, opensquilla.teammate.mailbox\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
