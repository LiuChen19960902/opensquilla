"""Teammate mailbox / executor / runtime 队列背压（queue backpressure）测试。

覆盖验收点：
1. ``Mailbox.mark_unread``：
   - 3 条消息 read_unread() 后全部已读，mark_unread([msg1, msg3]) 只恢复这两条，
     文件内列表长度不变（不复制、不膨胀 —— 与 requeue 的 re-append 语义区分）；
   - mark_unread 按 (from, text, timestamp) 三元组匹配，与对象身份 / read 标志无关。
2. ``Mailbox.merge_messages``（队列满实际落地的恢复原语）：
   - 积压批次合并成一个未读 envelope（``[QUEUED TEAM MESSAGE]`` 分隔、type=message、
     from=第一条的 from、read=False），单条批次走 mark_unread；
   - 并发 send 后 merge 不误配（只合并本批，不吞并发新消息）。
3. ``TeammateLLMExecutor.run_turn`` 队列满：mock task_runtime.enqueue 抛
   ``TaskQueueFullError(session_key='x', max_pending=64)``，run_turn 返回
   ``TURN_QUEUE_FULL`` 哨兵（不抛异常、不留 pending）。
4. runtime 处理循环：mock executor.run_turn 返回 ``TURN_QUEUE_FULL``，
   断言消息不被丢失（合并为未读 queued envelope，read=False）、handle 状态
   不是 error、且循环 break（后续消息未被处理）；合并后下轮 poll 只读到 1 条。
5. 回归：mock run_turn 正常返回文本时，消息照常全部处理（processed_count 递增，
   既有路径不被破坏）；背压缓解后下一个 poll 把合并 envelope 当单个 turn 重试。
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

from opensquilla.engine.teammate import TeammateManager
from opensquilla.gateway.task_runtime import TaskQueueFullError
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL, TeammateLLMExecutor
from opensquilla.teammate.mailbox import (
    QUEUED_TEAM_MESSAGE_SEPARATOR,
    Mailbox,
    MailboxMessage,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import TeammateRuntime


# ── sandbox event-loop shim ────────────────────────────────────────────
# 某些沙箱（seccomp）禁 socket.socketpair()，默认 SelectorEventLoop 无法创建。
# 此处仅在 socketpair 被禁时启用 os.pipe 版事件循环；正常机器是严格 no-op。
class _PipeEventLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self) -> None:
        self._ssock, self._wsock = os.pipe()
        os.set_blocking(self._ssock, False)
        os.set_blocking(self._wsock, False)

    def _close_self_pipe(self) -> None:
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
            self._process_self_data(data)  # type: ignore[attr-defined]  # private hook
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


if not _socketpair_available():
    asyncio.set_event_loop_policy(_PipeEventLoopPolicy())


# ── helpers ────────────────────────────────────────────────────────────


def _make_world(tmp_path: Path) -> tuple[TeammateManager, Any, str]:
    registry = TeamRegistry(tmp_path / "teams")
    manager = TeammateManager(registry)
    team = manager.registry.create_team(
        "team-a", lead_agent_id="main", lead_session_key="lead-session"
    )
    manager.spawn_teammate(team.id, "alice", "you are alice")
    handle = manager.handles.get_by_agent_id(team.id, "alice")
    assert handle is not None
    return manager, handle, team.id


def _inbox(manager: TeammateManager, team_id: str, name: str) -> Mailbox:
    handle = manager.handles.get_by_agent_id(team_id, name)
    assert handle is not None
    return handle.mailbox


# ── 1. mailbox.mark_unread ─────────────────────────────────────────────


def test_mark_unread_restores_only_selected_messages(tmp_path: Path) -> None:
    """mark_unread([msg1, msg3]) 只恢复这两条，文件列表长度不变（不复制不膨胀）。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "m1"})
    mb.send("team-lead", {"type": "message", "text": "m2"})
    mb.send("peer", {"type": "message", "text": "m3"})

    msgs = mb.read_unread()
    assert len(msgs) == 3
    assert mb.count_unread() == 0

    restored = mb.mark_unread([msgs[0], msgs[2]])
    assert restored == 2

    after = mb.peek()
    assert len(after) == 3, "文件内列表长度不变（不复制、不膨胀）"
    assert [m.read for m in after] == [False, True, False], "只恢复 1、3 两条"


def test_mark_unread_matches_by_from_text_timestamp_triple(tmp_path: Path) -> None:
    """mark_unread 按 (from, text, timestamp) 三元组匹配，与对象身份/read 标志无关。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "hello"})
    mb.send("peer", {"type": "message", "text": "world"})
    mb.read_unread()
    assert mb.count_unread() == 0

    orig = mb.peek()
    # 重建副本：三元组相同但对象身份不同、read 标志为 True（模拟跨进程/序列化拷贝）。
    copy1 = MailboxMessage(
        from_name=orig[0].from_name,
        text=orig[0].text,
        timestamp=orig[0].timestamp,
        read=True,
    )
    restored = mb.mark_unread([copy1])

    assert restored == 1, "应恢复与三元组匹配的那一条（与对象身份/read 标志无关）"
    assert mb.count_unread() == 1
    after = mb.peek()
    assert len(after) == 2, "文件列表长度不得变化"
    assert after[0].read is False, "第一条（匹配三元组）应恢复未读"
    assert after[1].read is True, "第二条（三元组不匹配）应保持已读"


def test_mark_unread_empty_and_unknown_are_noops(tmp_path: Path) -> None:
    """空列表与未知三元组都是 no-op：不改变 read 状态、不膨胀文件。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "only"})
    msgs = mb.read_unread()

    assert mb.mark_unread([]) == 0
    assert mb.count_unread() == 0
    assert len(mb.peek()) == 1

    ghost = MailboxMessage(
        from_name="nobody", text="missing", timestamp="2000-01-01T00:00:00+00:00"
    )
    restored = mb.mark_unread([msgs[0], ghost])
    assert restored == 1, "已知消息恢复未读，未知三元组被忽略"
    assert mb.count_unread() == 1
    assert len(mb.peek()) == 1, "未知三元组不得追加新消息"


# ── 2. mailbox.merge_messages ──────────────────────────────────────────


def test_merge_messages_coalesces_backlog_into_single_unread_envelope(
    tmp_path: Path,
) -> None:
    """3 条积压 → 合并成 1 条（text 含全部原文、type=message、from=首条、read=False）。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "m1"})
    mb.send("team-lead", {"type": "message", "text": "m2"})
    mb.send("peer", {"type": "message", "text": "m3"})

    batch = mb.read_unread()
    merged = mb.merge_messages(batch)

    assert merged is not None
    assert merged.read is False, "合并 envelope 必须未读，下个 poll 才会处理"
    assert merged.type == "message"
    assert merged.from_name == "team-lead", "from 取第一条的 from"
    for i in ("m1", "m2", "m3"):
        assert i in merged.text, "text 必须包含全部原文"
    after = mb.peek()
    assert len(after) == 1, "批次合并成一个 envelope：文件列表不膨胀"
    assert QUEUED_TEAM_MESSAGE_SEPARATOR in after[0].text
    assert after[0].read is False


def test_merge_messages_single_message_just_marks_unread(tmp_path: Path) -> None:
    """只剩 1 条时不合并，直接恢复未读（mark_unread 语义）。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "only"})
    batch = mb.read_unread()
    assert mb.count_unread() == 0

    merged = mb.merge_messages(batch)

    assert merged is not None
    assert merged is batch[0], "单条批次返回原对象（不合并）"
    after = mb.peek()
    assert len(after) == 1
    assert after[0].read is False, "单条直接恢复未读"


def test_merge_messages_empty_batch_returns_none(tmp_path: Path) -> None:
    """空批次 no-op：返回 None、不改变文件。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "m1"})
    mb.read_unread()

    assert mb.merge_messages([]) is None
    assert len(mb.peek()) == 1


def test_merge_does_not_touch_concurrent_new_message(tmp_path: Path) -> None:
    """并发 send 后 merge 不误配：只合并本批，不吞并发新消息。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "text": "m1"})
    mb.send("team-lead", {"type": "message", "text": "m2"})

    batch = mb.read_unread()
    # 处理本批期间，并发到达一条新消息（未读）。
    mb.send("peer", {"type": "message", "text": "concurrent"})

    mb.merge_messages(batch[1:])  # 只合并本批的 m2

    texts = [m.text for m in mb.peek()]
    assert len(mb.peek()) == 3, "m1(已读) + m2(合并回未读) + concurrent(未读) = 3"
    assert any("m1" in t for t in texts), "并发新消息不得顶掉 m1"
    assert any("concurrent" in t for t in texts), "并发新消息必须原样保留"
    assert any("m2" in t for t in texts), "本批 m2 合并后仍在"


# ── 3. llm_executor sentinel ───────────────────────────────────────────


async def test_run_turn_returns_sentinel_on_queue_full(tmp_path: Path) -> None:
    """run_turn 队列满：enqueue 抛 TaskQueueFullError → 返回 TURN_QUEUE_FULL 哨兵。"""
    manager, handle, _ = _make_world(tmp_path)

    class _FullRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def enqueue(self, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise TaskQueueFullError(session_key="x", max_pending=64)

    class _FakeSessions:
        async def get_or_create(self, *args: Any, **kwargs: Any) -> tuple[Any, bool]:
            return object(), False

        async def append_message(self, *args: Any, **kwargs: Any) -> None:
            return None

    runtime = _FullRuntime()
    executor = TeammateLLMExecutor(
        task_runtime=runtime,  # type: ignore[arg-type]
        session_manager=_FakeSessions(),  # type: ignore[arg-type]
    )
    msg = MailboxMessage(from_name="team-lead", text="x", timestamp="t", type="message")
    result = await executor.run_turn(manager, handle, msg)

    assert result is TURN_QUEUE_FULL, "队列满应返回哨兵而不是抛异常"
    assert runtime.calls == 1, "enqueue 只调用一次，不重试"


# ── 4. runtime sentinel path ───────────────────────────────────────────


class _QueueFullExecutor:
    """run_turn 始终返回 TURN_QUEUE_FULL 的假 executor。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, manager: Any, handle: Any, message: Any) -> Any:
        self.calls += 1
        return TURN_QUEUE_FULL


async def test_runtime_queue_full_restores_unread_and_breaks(tmp_path: Path) -> None:
    """run_turn 返回 TURN_QUEUE_FULL → 消息不丢失、handle 非 error、循环 break。

    落地实现按 pi-subagents 风格把失败批次合并成一个未读 queued envelope
    （``merge_messages``）：消息不丢失、handle 不进 error、循环立即 break。
    """
    manager, handle, team_id = _make_world(tmp_path)
    inbox = _inbox(manager, team_id, "alice")
    inbox.send("team-lead", {"type": "message", "text": "m1"})
    inbox.send("team-lead", {"type": "message", "text": "m2"})
    inbox.send("peer", {"type": "message", "text": "m3"})

    executor = _QueueFullExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)
    replies = await runtime.poll_once(team_id)

    assert executor.calls == 1, "队列满时应立即 break，后续消息不得再调用 run_turn"
    assert replies == [], "队列满不应产生任何回复"
    msgs = inbox.peek()
    assert len(msgs) == 1, "合并后应只剩一个 queued envelope（不复制、不膨胀）"
    assert msgs[0].read is False, "合并 envelope 应为未读，下个 poll 重试"
    assert "m1" in msgs[0].text and "m2" in msgs[0].text and "m3" in msgs[0].text
    assert inbox.count_unread() == 1
    assert handle.status != "error", "队列满是背压信号，不是 handler 错误，不得进入 error"
    assert handle.stop_reason == ""


# ── 5. 回归：正常路径不破坏 ────────────────────────────────────────────


class _AckExecutor:
    """run_turn 正常返回文本的假 executor。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, manager: Any, handle: Any, message: Any) -> str:
        self.calls += 1
        return f"ack-{self.calls}"


async def test_runtime_normal_reply_path_still_works(tmp_path: Path) -> None:
    """回归：run_turn 正常返回文本时，消息照常全部处理（processed_count 递增）。"""
    manager, handle, team_id = _make_world(tmp_path)
    inbox = _inbox(manager, team_id, "alice")
    inbox.send("team-lead", {"type": "message", "text": "m1"})
    inbox.send("team-lead", {"type": "message", "text": "m2"})

    executor = _AckExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)
    replies = await runtime.poll_once(team_id)

    assert executor.calls == 2, "两条消息都应被处理（processed_count 递增）"
    assert len(replies) == 2
    assert inbox.count_unread() == 0, "正常处理后消息应保持已读（不误恢复未读）"
    assert handle.status == "idle", "正常处理后应回到 idle"


class _FlakyQueueFullExecutor:
    """第一次 run_turn 返回 TURN_QUEUE_FULL，之后正常返回文本。

    模拟背压缓解后的重试：首个 poll 队列满合并，第二个 poll 把合并后的
    envelope 当单个 turn 处理（不丢失、不重复）。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, manager: Any, handle: Any, message: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return TURN_QUEUE_FULL
        return "finally-acked"


async def test_runtime_queue_full_then_retry_processes_merged_envelope(
    tmp_path: Path,
) -> None:
    """回归：背压缓解后，下轮 poll 把合并 envelope 当单个 turn 重试（消息数收敛）。"""
    manager, handle, team_id = _make_world(tmp_path)
    inbox = _inbox(manager, team_id, "alice")
    inbox.send("team-lead", {"type": "message", "text": "m1"})
    inbox.send("team-lead", {"type": "message", "text": "m2"})
    inbox.send("peer", {"type": "message", "text": "m3"})

    executor = _FlakyQueueFullExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)

    # poll 1：队列满 → 3 条合并成 1 个未读 envelope。
    replies1 = await runtime.poll_once(team_id)
    assert executor.calls == 1
    assert replies1 == []
    assert inbox.count_unread() == 1

    # 消息数收敛：磁盘上只剩 1 个未读 envelope（peek 不消费，确保 poll 2 读到）。
    msgs = inbox.peek()
    assert len(msgs) == 1, "合并后消息数收敛为 1（3 条积压 → 1 个 envelope）"
    assert QUEUED_TEAM_MESSAGE_SEPARATOR in msgs[0].text

    # poll 2：合并 envelope 作为单个 turn 处理（1 → 0 收敛）。
    replies2 = await runtime.poll_once(team_id)
    assert executor.calls == 2, "重试只应再处理一次（合并后的单个 turn）"
    assert replies2 == ["alice: finally-acked"]
    assert inbox.count_unread() == 0
