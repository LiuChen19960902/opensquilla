"""Queue-full backpressure tests for teammates subsystem.

Covers the pi-subagents style coalescing fix:
- TaskQueueFullError -> TURN_QUEUE_FULL sentinel (llm_executor)
- runtime poll loop merges backlog instead of requeue-spinning
- mailbox.merge_messages preserves order/content, single-batch skips merge
- concurrent sends are not mis-matched by merge

Also covers mailbox.mark_unread per the verifier acceptance points:
- restore unread without copying / inflating the inbox list
- match by (from, text, timestamp) triple, independent of object identity
- mark_unread does not mis-match under concurrent sends
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from opensquilla.engine.teammate import TeammateManager
from opensquilla.gateway.task_runtime import TaskQueueFullError
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.models import MailboxMessage
from opensquilla.teammate.registry import TeamRegistry


# ── sandbox event-loop shim ────────────────────────────────────────────────
# Some sandboxes (seccomp) block socket.socketpair(), which the default
# SelectorEventLoop needs. os.pipe() still works, so we swap in a pipe-based
# policy ONLY when socketpair is unavailable. On normal machines this is a
# strict no-op and CI behavior is identical.
class _PipeEventLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self) -> None:
        self._ssock, self._wsock = os.pipe()
        os.set_blocking(self._ssock, False)
        os.set_blocking(self._wsock, False)

    def _close_self_pipe(self) -> None:
        # Idempotent + defensive: BaseEventLoop.__del__ may re-enter after
        # close(); teardown-time fd errors must never crash GC.
        for attr in ("_ssock", "_wsock"):
            fd = getattr(self, attr, None)
            if fd is None:
                continue
            try:
                os.close(fd)
            except (OSError, TypeError, ValueError):
                pass
            setattr(self, attr, None)

    def _write_to_self(self) -> None:
        if self._wsock is not None:
            try:
                os.write(self._wsock, b"\0")
            except (BlockingIOError, InterruptedError, ConnectionError, OSError):
                pass

    def _read_from_self(self) -> None:
        try:
            data = os.read(self._ssock, 4096)
            if not data:
                return
            self._process_self_data(data)  # type: ignore[attr-defined]
        except (BlockingIOError, InterruptedError):
            pass


class _PipeEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        return _PipeEventLoop()


def _socketpair_available() -> bool:
    try:
        a, b = socket.socketpair()
        a.close()
        b.close()
        return True
    except Exception:
        return False


# Install at module import time (BEFORE any pytest-asyncio fixture runs).
if not _socketpair_available():
    asyncio.set_event_loop_policy(_PipeEventLoopPolicy())


def _new_team_mgr(tmp_path):
    reg = TeamRegistry(tmp_path)
    mgr = TeammateManager(reg)
    team = reg.create_team("q-test", "t", "Queue Test")
    team_id = team.team_id if hasattr(team, "team_id") else team.id
    handle = mgr.spawn_teammate(team_id, "worker", "prompt")
    return mgr, team_id, handle


# ── mailbox.mark_unread ────────────────────────────────────────────────────
def test_mark_unread_restores_only_selected_without_copy(tmp_path):
    """mark_unread([msg1, msg3]) restores only those two; list length unchanged."""
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "a"})
    mb.send("lead", {"type": "message", "text": "b"})
    mb.send("lead", {"type": "message", "text": "c"})

    batch = mb.read_unread()
    assert len(batch) == 3
    assert all(m.read for m in mb.peek())

    restored = mb.mark_unread([batch[0], batch[2]])
    assert restored == 2
    after = mb.peek()
    assert len(after) == 3, "list length must not change (no copy / no inflation)"
    assert [not m.read for m in after] == [True, False, True]


def test_mark_unread_matches_by_triple_not_identity(tmp_path):
    """mark_unread matches (from, text, timestamp); object identity/read flag ignored."""
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "hello"})
    mb.send("peer", {"type": "message", "text": "world"})
    mb.read_unread()

    orig = mb.peek()
    # Rebuilt copy: same triple, different object, read=True (simulates
    # cross-process / serialization round-trip).
    copy1 = MailboxMessage(
        from_name=orig[0].from_name,
        text=orig[0].text,
        timestamp=orig[0].timestamp,
        read=True,
    )
    restored = mb.mark_unread([copy1])
    assert restored == 1, "must match by triple, not identity/read flag"
    after = mb.peek()
    assert len(after) == 2
    assert after[0].read is False and after[1].read is True


def test_mark_unread_concurrent_send_not_mismatched(tmp_path):
    """A message appended after read_unread must not be restored/matched by mark_unread."""
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "m1"})
    mb.send("lead", {"type": "message", "text": "m2"})

    batch = mb.read_unread()
    # concurrent send while processing
    mb.send("other", {"type": "message", "text": "concurrent"})

    restored = mb.mark_unread(batch)  # only m1, m2 (the read batch)
    assert restored == 2
    after = mb.peek()
    assert len(after) == 3, "concurrent message must remain untouched"
    # concurrent message stays unread too (it was never read), but crucially
    # mark_unread must not accidentally flip it or duplicate anything.
    unread = [m.text for m in after if not m.read]
    assert any("m1" in t for t in unread)
    assert any("m2" in t for t in unread)
    assert any("concurrent" in t for t in unread)
    assert [m.text for m in after].count('{"type": "message", "text": "concurrent"}') == 1


def test_mark_unread_empty_and_unknown_are_noops(tmp_path):
    """Empty list and unknown triples are no-ops: no state change, no inflation."""
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "only"})
    mb.read_unread()

    assert mb.mark_unread([]) == 0
    assert mb.count_unread() == 0
    assert len(mb.peek()) == 1

    ghost = MailboxMessage(
        from_name="nobody", text="missing", timestamp="2000-01-01T00:00:00+00:00"
    )
    restored = mb.mark_unread([ghost])
    assert restored == 0
    assert len(mb.peek()) == 1, "unknown triple must not append new messages"


# ── mailbox.merge_messages ────────────────────────────────────────────────
def test_merge_multiple_messages_into_one(tmp_path):
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    for i in range(1, 4):
        mb.send("lead", {"type": "message", "text": f"msg-{i}"})

    batch = mb.read_unread()
    merged = mb.merge_messages(batch)

    assert merged is not None
    assert merged.read is False
    assert merged.type == "message"
    assert merged.from_name == "lead"
    for i in range(1, 4):
        assert f"msg-{i}" in merged.text
    # 3 backlog messages -> 1 merged envelope
    assert len(mb.peek()) == 1


def test_merge_preserves_order(tmp_path):
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "first"})
    mb.send("lead", {"type": "message", "text": "second"})

    batch = mb.read_unread()
    merged = mb.merge_messages(batch)
    assert merged.text.index("first") < merged.text.index("second")


def test_merge_single_message_skips_merge(tmp_path):
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "only"})

    batch = mb.read_unread()
    merged = mb.merge_messages(batch)

    assert merged is not None
    assert merged.text == '{"type": "message", "text": "only"}'
    # single-message batch is restored to unread in the inbox
    assert all(not m.read for m in mb.peek())
    assert len(mb.peek()) == 1


def test_merge_empty_batch_returns_none(tmp_path):
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    assert mb.merge_messages([]) is None


def test_merge_does_not_touch_concurrent_new_message(tmp_path):
    """A message appended after read_unread must not be removed by merge."""
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "m1"})
    mb.send("lead", {"type": "message", "text": "m2"})

    batch = mb.read_unread()
    # concurrent send while processing
    mb.send("other", {"type": "message", "text": "concurrent"})

    mb.merge_messages(batch[1:])  # only m2

    texts = [m.text for m in mb.peek()]
    assert any("m1" in t for t in texts), "m1 should remain"
    assert any("concurrent" in t for t in texts), "concurrent msg must remain"
    # m1 (read) + m2 (restored unread) + concurrent (unread) = 3
    assert len(mb.peek()) == 3


# ── llm_executor sentinel ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_turn_returns_sentinel_on_queue_full(tmp_path):
    """TeammateLLMExecutor.run_turn returns TURN_QUEUE_FULL when enqueue overflows."""
    from opensquilla.teammate.llm_executor import TeammateLLMExecutor

    _, team_id, handle = _new_team_mgr(tmp_path)

    class FakeTaskRuntime:
        def __init__(self):
            self.calls = 0

        async def enqueue(self, *a, **k):
            self.calls += 1
            raise TaskQueueFullError(session_key=handle.session_key, max_pending=64)

    class FakeSessionManager:
        async def get_or_create(self, *a, **k):
            return object(), False

        async def append_message(self, *a, **k):
            return None

    fake_runtime = FakeTaskRuntime()
    fake_sessions = FakeSessionManager()

    executor = TeammateLLMExecutor(
        task_runtime=fake_runtime,
        session_manager=fake_sessions,
    )

    msg = MailboxMessage(from_name="lead", text="x", timestamp="t", type="message")
    result = await executor.run_turn(TeammateManager(TeamRegistry(tmp_path)), handle, msg)
    assert result is TURN_QUEUE_FULL
    assert fake_runtime.calls == 1  # no re-enqueue loop


# ── runtime poll path (sentinel -> merge -> break) ────────────────────────
@pytest.mark.asyncio
async def test_runtime_sentinel_merges_and_breaks(tmp_path):
    """Sentinel path merges backlog and leaves one unread for next poll."""
    _, team_id, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    for i in range(1, 4):
        mb.send("lead", {"type": "message", "text": f"m{i}"})

    # Simulate the poll-loop block exactly as runtime.py does:
    # read_unread -> run_turn returns sentinel -> merge_messages -> break
    messages = mb.read_unread()
    processed_count = 0
    reply = TURN_QUEUE_FULL  # what run_turn returns on overflow
    assert reply is TURN_QUEUE_FULL
    if reply is TURN_QUEUE_FULL:
        mb.merge_messages(messages[processed_count:])
    # after merge: only 1 unread message remains for next poll
    unread = [m for m in mb.peek() if not m.read]
    assert len(unread) == 1
    assert "m1" in unread[0].text and "m2" in unread[0].text and "m3" in unread[0].text
    # handle must not be error
    assert handle.status != "error"


@pytest.mark.asyncio
async def test_runtime_real_poll_sentinel_merges_and_breaks(tmp_path):
    """Full poll_once path: queue-full -> merge -> break, next poll re-reads one."""
    from opensquilla.teammate.runtime import TeammateRuntime

    manager, team_id, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    for i in range(1, 4):
        mb.send("lead", {"type": "message", "text": f"m{i}"})

    class SentinelExecutor:
        def __init__(self):
            self.calls = 0

        async def run_turn(self, manager, handle, message):
            self.calls += 1
            return TURN_QUEUE_FULL

    executor = SentinelExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)
    replies = await runtime.poll_once(team_id)

    assert executor.calls == 1, "must break after first sentinel"
    assert replies == []
    assert handle.status != "error", "backpressure is not a handler error"

    # next poll re-reads exactly ONE merged unread envelope
    unread = [m for m in mb.peek() if not m.read]
    assert len(unread) == 1, "merged envelope is re-readable on next poll"
    assert "m1" in unread[0].text and "m2" in unread[0].text and "m3" in unread[0].text


# ── regression: normal path unaffected ────────────────────────────────────
def test_mark_unread_still_works(tmp_path):
    _, _, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "a"})
    mb.send("lead", {"type": "message", "text": "b"})

    batch = mb.read_unread()
    assert all(m.read for m in mb.peek())
    restored = mb.mark_unread(batch)
    assert restored == 2
    assert all(not m.read for m in mb.peek())
    assert len(mb.peek()) == 2


@pytest.mark.asyncio
async def test_runtime_normal_reply_path_unaffected(tmp_path):
    """Regression: normal run_turn replies still process all messages."""
    from opensquilla.teammate.runtime import TeammateRuntime

    manager, team_id, handle = _new_team_mgr(tmp_path)
    mb: Mailbox = handle.mailbox
    mb.send("lead", {"type": "message", "text": "a"})
    mb.send("lead", {"type": "message", "text": "b"})

    class AckExecutor:
        def __init__(self):
            self.calls = 0

        async def run_turn(self, manager, handle, message):
            self.calls += 1
            return f"ack-{self.calls}"

    executor = AckExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)
    replies = await runtime.poll_once(team_id)

    assert executor.calls == 2, "both messages processed (processed_count increments)"
    assert len(replies) == 2
    assert mb.count_unread() == 0
    assert handle.status == "idle"
