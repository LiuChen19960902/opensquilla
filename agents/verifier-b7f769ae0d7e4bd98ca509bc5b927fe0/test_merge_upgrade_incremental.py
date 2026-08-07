"""merge_messages 升级验收点补充测试（增量，供合并进 test_queue_backpressure.py）。

覆盖 19:33 指令中现有文件缺失的验收点：
- 验收点 1 补强：合并 envelope 的 type=message 断言
- 验收点 3：合并后下轮 poll 只读到 1 条（消息数收敛）
- 验收点 4：并发 send 后 merge 不误配（只合并本批、不吞并发新消息）
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

from opensquilla.engine.teammate import TeammateManager
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import QUEUED_TEAM_MESSAGE_SEPARATOR, Mailbox, MailboxMessage
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import TeammateRuntime

# ── 沙箱垫片（与现有文件相同）：seccomp 禁 socketpair 时用 os.pipe 事件循环 ──


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


if not _socketpair_available():
    asyncio.set_event_loop_policy(_PipeEventLoopPolicy())


# ── helpers（与现有文件一致）──────────────────────────────────────────


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
    from opensquilla.engine.teammate import team_dir_of

    return Mailbox.open(team_dir_of(manager.registry, team_id), name)


# ── 验收点 1 补强：合并 envelope 的 type=message ────────────────────────


def test_merge_coalesced_envelope_has_type_message(tmp_path: Path) -> None:
    """合并 envelope 的 type 必须为 message（验收点 1）。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "task_assignment", "text": "m1"})
    mb.send("peer", {"type": "message", "text": "m2"})
    msgs = mb.read_unread()

    merged = mb.merge_messages(msgs)

    assert merged is not None
    assert merged.type == "message", "合并 envelope 的 type 必须是 message"
    after = mb.peek()
    assert len(after) == 1
    assert after[0].type == "message"


# ── 验收点 3：合并后下轮 poll 只读到 1 条（消息数收敛）──────────────────


class _FlakyQueueFullExecutor:
    """第一次 run_turn 返回 TURN_QUEUE_FULL，之后正常返回文本。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, manager: Any, handle: Any, message: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return TURN_QUEUE_FULL
        return "finally-acked"


async def test_runtime_after_merge_next_poll_reads_single_message(
    tmp_path: Path,
) -> None:
    """合并后下轮 poll 只读到 1 条：3 条积压收敛为 1 个未读 envelope（验收点 3）。"""
    manager, handle, team_id = _make_world(tmp_path)
    inbox = _inbox(manager, team_id, "alice")
    inbox.send("team-lead", {"type": "message", "text": "m1"})
    inbox.send("team-lead", {"type": "message", "text": "m2"})
    inbox.send("peer", {"type": "message", "text": "m3"})

    executor = _FlakyQueueFullExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)

    # poll 1：队列满 → 合并成一个未读 envelope。
    await runtime.poll_once(team_id)
    assert inbox.count_unread() == 1

    # 关键断言：消息数收敛——磁盘上只剩 1 个未读 envelope（不消费它，
    # 用 peek 验证，确保 poll 2 仍能读到）。
    msgs = inbox.peek()
    assert len(msgs) == 1, "合并后消息数收敛为 1（3 条积压 → 1 个 envelope）"
    assert msgs[0].read is False
    assert "m1" in msgs[0].text
    assert "m2" in msgs[0].text
    assert "m3" in msgs[0].text
    assert QUEUED_TEAM_MESSAGE_SEPARATOR in msgs[0].text

    # poll 2：合并 envelope 作为单个 turn 处理（消息数从 1 → 0 收敛）。
    replies2 = await runtime.poll_once(team_id)
    assert executor.calls == 2, "重试只应再处理一次（合并后的单个 turn）"
    assert replies2 == ["alice: finally-acked"]
    assert inbox.count_unread() == 0


# ── 验收点 4：并发 send 后 merge 不误配 ─────────────────────────────────


def test_merge_does_not_absorb_concurrent_new_message(tmp_path: Path) -> None:
    """并发 send 后 merge 只合并本批，不吞并发新消息（验收点 4）。"""
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("lead", {"type": "message", "text": "m1"})
    mb.send("lead", {"type": "message", "text": "m2"})

    batch = mb.read_unread()  # 本批 = [m1, m2]（已读）

    # 并发：处理本批期间到达的新消息（未读）。
    mb.send("peer", {"type": "message", "text": "concurrent"})

    # 只合并本批中的 m2（模拟 processed_count 之后的尾部）。
    merged = mb.merge_messages([batch[1]])

    assert merged is not None
    after = mb.peek()
    assert len(after) == 3, "并发新消息不得被吞：m1(已读) + 合并envelope + concurrent"
    texts = [m.text for m in after]
    assert any("m1" in t for t in texts), "已处理的 m1 应保留"
    assert any("concurrent" in t for t in texts), "并发新消息必须原样保留"
    # 并发消息保持未读且未被合并进 envelope。
    conc = next(m for m in after if "concurrent" in m.text)
    assert conc.read is False
    assert "concurrent" not in merged.text, "合并 envelope 不得吞入并发新消息"
    assert mb.count_unread() == 2, "合并envelope(未读) + 并发消息(未读)"


def test_merge_empty_and_unknown_batch_noop(tmp_path: Path) -> None:
    """空批次 no-op；未知批次不产生任何写入（验收点 4 边界）。

    注意实现细节：单条未知批次走 mark_unread 分支返回原对象（但不写盘），
    多条未知批次在磁盘上找不到时返回 None。两者都不应改变文件内容。
    """
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("lead", {"type": "message", "text": "m1"})
    mb.read_unread()

    assert mb.merge_messages([]) is None, "空批次返回 None"
    assert len(mb.peek()) == 1

    unknown = MailboxMessage(
        from_name="nobody", text="missing", timestamp="2000-01-01T00:00:00+00:00"
    )
    result = mb.merge_messages([unknown])  # 单条批次 → mark_unread 分支
    assert result is unknown, "单条未知批次返回原对象（mark_unread 语义）"
    assert len(mb.peek()) == 1, "未知批次不得产生任何写入/膨胀"
    assert mb.peek()[0].read is True, "未知三元组不得把已有消息误恢复未读"
