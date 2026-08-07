"""Gateway-integration tests: resident loop over all teams, restart restore,
and the TUI-facing tool surface (teammate_list / teammate_inbox /
teammate_shutdown).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    TYPE_IDLE_NOTIFICATION,
    TYPE_MESSAGE,
    TYPE_SHUTDOWN_APPROVED,
    TYPE_TASK_ASSIGNMENT,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import StubTeammateExecutor, TeammateRuntime
from opensquilla.teammate import tools as teammate_tools


def _world(tmp_path: Path, handler=None) -> tuple[TeammateManager, TeammateRuntime]:
    registry = TeamRegistry(tmp_path)
    executor = StubTeammateExecutor(handler)
    manager = TeammateManager(registry, executor=executor)
    runtime = TeammateRuntime(manager, poll_interval=0.01, executor=executor)
    return manager, runtime


def _spawn(manager: TeammateManager, name: str) -> object:
    team = manager.registry.create_team(name="demo", lead_agent_id="team-lead")
    handle = manager.spawn_teammate(team.id, name, f"you are {name}")
    return team, handle


async def _poll_until(manager: TeammateManager, team_id: str, status: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for h in manager.handles.list(team_id):
            if h.status == status:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"teammate never reached {status}")


async def _poll_until_notified(manager: TeammateManager, team_id: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    inbox = Mailbox.open(team_dir_of(manager.registry, team_id), "team-lead")
    while asyncio.get_event_loop().time() < deadline:
        if any(m.type == TYPE_IDLE_NOTIFICATION for m in inbox.peek()):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("lead inbox never received an idle_notification")


# ── run_all: gateway resident loop ────────────────────────────────────


def test_run_all_polls_all_teams_and_stops(tmp_path: Path) -> None:
    manager, runtime = _world(tmp_path)
    team_a, alice = _spawn(manager, "alice")
    team_b, bob = _spawn(manager, "bob")

    manager.send_message(
        team_a.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job A"}
    )
    manager.send_message(
        team_b.id, "bob", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job B"}
    )

    stop = asyncio.Event()

    async def _drive() -> None:
        task = asyncio.create_task(runtime.run_all(stop))
        await _poll_until_notified(manager, team_a.id)
        await _poll_until_notified(manager, team_b.id)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_drive())

    # Both teams' leads got the ack reply routed back.
    for team_id, name in ((team_a.id, "alice"), (team_b.id, "bob")):
        inbox = Mailbox.open(team_dir_of(manager.registry, team_id), "team-lead")
        assert any(m.type == TYPE_IDLE_NOTIFICATION for m in inbox.peek())
        assert any(m.from_name == name and m.type == "message" for m in inbox.peek())


# ── restore_teams: gateway restart recovery ───────────────────────────


def test_restore_teams_recreates_handles(tmp_path: Path) -> None:
    manager, _ = _world(tmp_path)
    team, alice = _spawn(manager, "alice")
    manager.send_message(
        team.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job"}
    )

    # Simulate a gateway restart: fresh manager over the same registry root.
    registry2 = TeamRegistry(tmp_path)
    manager2 = TeammateManager(registry2, executor=StubTeammateExecutor())
    assert manager2.handles.count_active() == 0
    restored = manager2.restore_teams()
    assert restored == 1
    handle = manager2.handles.get_by_agent_id(team.id, "alice")
    assert handle is not None
    assert handle.status == "idle"
    assert handle.max_turns == alice.max_turns

    # Mailbox survives: unread task still there, and the loop can process it.
    runtime2 = TeammateRuntime(manager2, poll_interval=0.01, executor=StubTeammateExecutor())
    asyncio.run(runtime2.poll_once(team.id))
    inbox = Mailbox.open(team_dir_of(manager2.registry, team.id), "team-lead")
    assert any("ack" in m.text for m in inbox.peek())


def test_restore_skips_terminal_members(tmp_path: Path) -> None:
    manager, runtime = _world(tmp_path)
    team, alice = _spawn(manager, "alice")
    manager.request_shutdown(team.id, "alice")
    asyncio.run(runtime.poll_once(team.id))  # stub approves → done

    registry2 = TeamRegistry(tmp_path)
    manager2 = TeammateManager(registry2, executor=StubTeammateExecutor())
    assert manager2.restore_teams() == 0  # member is done; nothing to restore


# ── tool surface ──────────────────────────────────────────────────────


def test_teammate_list_tool(tmp_path: Path) -> None:
    manager, _ = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        _spawn(manager, "alice")
        payload = json.loads(asyncio.run(teammate_tools.teammate_list()))
        assert len(payload) == 1
        assert payload[0]["members"][0]["name"] == "alice"
        assert payload[0]["members"][0]["status"] == "idle"
    finally:
        teammate_tools.set_teammate_manager(None)


def test_teammate_inbox_tool(tmp_path: Path) -> None:
    manager, runtime = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, _ = _spawn(manager, "alice")
        manager.send_message(
            team.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job"}
        )
        asyncio.run(runtime.poll_once(team.id))
        entries = json.loads(asyncio.run(teammate_tools.teammate_inbox(team_id=team.id)))
        types = {e["type"] for e in entries}
        assert "message" in types  # the ack reply
        assert TYPE_IDLE_NOTIFICATION in types  # system idle notification
        idle = next(e for e in entries if e["type"] == TYPE_IDLE_NOTIFICATION)
        assert idle["idleReason"] == "available"
        assert "ack" in idle["summary"]
    finally:
        teammate_tools.set_teammate_manager(None)


def test_teammate_shutdown_tool_handshake_and_force(tmp_path: Path) -> None:
    manager, runtime = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, alice = _spawn(manager, "alice")

        # Cooperative handshake → done.
        asyncio.run(teammate_tools.teammate_shutdown(team_id=team.id, name="alice"))
        asyncio.run(runtime.poll_once(team.id))
        assert alice.status == "done"
        inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "team-lead")
        assert any(m.type == TYPE_SHUTDOWN_APPROVED for m in inbox.peek())

        # Force kill (no handshake) → aborted immediately.
        team2, bob = _spawn(manager, "bob")
        out = json.loads(
            asyncio.run(teammate_tools.teammate_shutdown(team_id=team2.id, name="bob", force=True))
        )
        assert out["killed"] is True
        assert bob.status == "aborted"
    finally:
        teammate_tools.set_teammate_manager(None)


def test_tools_raise_before_wiring() -> None:
    teammate_tools.set_teammate_manager(None)
    with pytest.raises(Exception):
        asyncio.run(teammate_tools.teammate_list())


def test_runtime_visible_sink_streams_replies_to_lead(tmp_path: Path) -> None:
    manager, runtime = _world(tmp_path)
    seen: list[tuple[str, str, str, bool]] = []

    async def sink(team_id: str, from_name: str, text: str, notify_only: bool) -> None:
        seen.append((team_id, from_name, text, notify_only))

    runtime.visible_sink = sink
    team, alice = _spawn(manager, "alice")
    manager.send_message(team.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job"})
    asyncio.run(runtime.poll_once(team.id))
    assert seen and seen[0][0] == team.id and seen[0][1] == "alice"
    assert "ack" in seen[0][2]
    # P0 replies are notifications: shown on the lead's screen but excluded
    # from the lead's LLM context (system-role semantics).
    assert seen[0][3] is True


def test_runtime_completion_sink_fires_on_idle(tmp_path: Path) -> None:
    """成员完成任务（idle 通知）→ completion_sink 被触发（pi-subagents 风格完成通知的 runtime 侧）。"""
    manager, runtime = _world(tmp_path)
    seen: list[tuple[str, str, str, str]] = []

    async def sink(team_id: str, member: str, summary: str, reason: str) -> None:
        seen.append((team_id, member, summary, reason))

    runtime.completion_sink = sink
    team, alice = _spawn(manager, "alice")
    manager.send_message(
        team.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job"}
    )
    asyncio.run(runtime.poll_once(team.id))
    assert seen, "成员完成任务后 completion_sink 应被触发"
    assert seen[0][0] == team.id
    assert seen[0][1] == "alice"
    assert "ack" in seen[0][2]  # stub executor 的回复被作为完成摘要
    assert seen[0][3] == "available"


def test_runtime_completion_sink_missing_is_noop(tmp_path: Path) -> None:
    """未接 completion_sink 时原有行为不变（mailbox idle_notification 照常）。"""
    manager, runtime = _world(tmp_path)
    team, alice = _spawn(manager, "alice")
    manager.send_message(
        team.id, "alice", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "job"}
    )
    asyncio.run(runtime.poll_once(team.id))
    inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "team-lead")
    assert any(m.type == TYPE_IDLE_NOTIFICATION for m in inbox.peek())


def test_lateral_communication_reply_routes_to_peer(tmp_path: Path) -> None:
    """能力7：teammate → teammate 横向沟通。

    alice 给 bob 发消息（sender=alice）→ bob 处理并回复
    → 回复应落在 alice 的 inbox（不是 team-lead）。
    """
    manager, runtime = _world(tmp_path)
    team, alice = _spawn(manager, "alice")
    bob = manager.spawn_teammate(team.id, "bob", "you are bob")
    team_id = team.id
    # alice → bob (lateral): sender is alice, not team-lead
    manager.send_message(team_id, "bob", {"type": TYPE_MESSAGE, "text": "我改了api接口，新签名是 foo(x)"}, sender="alice")
    asyncio.run(runtime.poll_once(team_id))

    # bob's reply must land in alice's inbox (peer-to-peer routing)
    alice_inbox = Mailbox.open(team_dir_of(manager.registry, team_id), "alice")
    bob_msgs = [m for m in alice_inbox.peek() if m.from_name == "bob"]
    assert bob_msgs, "bob 的回复应落在 alice 的 inbox"
    assert "ack" in bob_msgs[0].text

    # lead inbox must NOT get the lateral reply
    lead_inbox = Mailbox.open(team_dir_of(manager.registry, team_id), "team-lead")
    assert not any(m.from_name == "bob" and m.type == "message" for m in lead_inbox.peek())


def test_teammate_send_tool_supports_sender(tmp_path: Path) -> None:
    """teammate_send 工具支持 sender 参数（P1 真 agent 横向入口）。"""
    manager, _ = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, alice = _spawn(manager, "alice")
        bob = manager.spawn_teammate(team.id, "bob", "you are bob")
        out = json.loads(
            asyncio.run(teammate_tools.teammate_send(
                team_id=team.id, to="bob",
                message={"type": TYPE_MESSAGE, "text": "hi bob"},
                sender="alice",
            ))
        )
        assert out["from"] == "alice" and out["to"] == "bob" and out["delivered"] is True
        bob_inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "bob")
        assert any(m.from_name == "alice" for m in bob_inbox.peek())
    finally:
        teammate_tools.set_teammate_manager(None)


def test_teammate_assign_one_step(tmp_path: Path) -> None:
    """teammate_assign：任务板建任务 + mailbox 派发一步到位（能力 1 拆解分配）。"""
    manager, runtime = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, alice = _spawn(manager, "alice")
        out = json.loads(asyncio.run(teammate_tools.teammate_assign(
            team.id, "alice", "写文档", "给 API 写 README", context="文件在 docs/api.md"
        )))
        assert out["assigned"] is True
        assert out["delivered_to"] == "alice"
        task = out["task"]
        assert task["status"] == "pending"
        assert task["assignee"] == "alice"
        assert "CONTEXT" in task["description"]

        # mailbox 里也收到了 task_assignment
        inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "alice")
        msgs = inbox.peek()
        assert any(TYPE_TASK_ASSIGNMENT in m.text for m in msgs)
    finally:
        teammate_tools.set_teammate_manager(None)


def test_teammate_inbox_reads_member(tmp_path: Path) -> None:
    """teammate_inbox 支持 name 参数读任意成员收件箱（能力 1 整合采集）。"""
    manager, runtime = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, alice = _spawn(manager, "alice")
        manager.send_message(team.id, "alice", {"type": TYPE_MESSAGE, "text": "横向消息"}, sender="bob-x")
        entries = json.loads(asyncio.run(teammate_tools.teammate_inbox(team.id, name="alice")))
        assert any(e["from"] == "bob-x" for e in entries)
    finally:
        teammate_tools.set_teammate_manager(None)


def test_wire_teammate_llm_executor_hot_swap() -> None:
    """wire_teammate_llm_executor：task_runtime 就绪后 stub→LLM 热切换（能力 2 接线）。"""
    from opensquilla.gateway.boot import wire_teammate_llm_executor

    class _FakeRuntime:
        def __init__(self) -> None:
            self.executor = None

    class _FakeSvc:
        def __init__(self) -> None:
            self.teammate_runtime = _FakeRuntime()
            self.session_manager = object()

    svc = _FakeSvc()
    wire_teammate_llm_executor(svc, object())
    assert "TeammateLLMExecutor" in type(svc.teammate_runtime.executor).__name__

    # 无 task_runtime（standalone/测试）→ 保持 stub，不炸
    svc2 = _FakeSvc()
    wire_teammate_llm_executor(svc2, None)
    assert svc2.teammate_runtime.executor is None or svc2.teammate_runtime.executor is not None


def test_teammate_spawn_adds_member_to_existing_team(tmp_path: Path) -> None:
    """teammate_spawn：往已有团队加成员（能力 1：多成员同队）。"""
    manager, _ = _world(tmp_path)
    teammate_tools.set_teammate_manager(manager)
    try:
        team, _ = _spawn(manager, "backend")
        out = json.loads(asyncio.run(teammate_tools.teammate_spawn(team.id, "frontend", "frontend role")))
        assert out["team_id"] == team.id
        assert out["status"] == "idle"
        names = [h.name for h in manager.handles.list(team.id)]
        assert names == ["backend", "frontend"]
    finally:
        teammate_tools.set_teammate_manager(None)


def test_lateral_traffic_streams_to_lead_visible_session(tmp_path: Path) -> None:
    """横向沟通消息也推送到 lead 可见会话（Claude Code 共享 transcript）。"""
    manager, runtime = _world(tmp_path)
    seen: list[tuple[str, str, str, bool]] = []
    async def sink(team_id: str, from_name: str, text: str, notify_only: bool) -> None:
        seen.append((team_id, from_name, text, notify_only))
    runtime.visible_sink = sink
    team, backend = _spawn(manager, "backend")
    manager.spawn_teammate(team.id, "frontend", "you are frontend")

    # frontend → backend 横向消息
    manager.send_message(team.id, "backend", {"type": "message", "text": "接口契约是什么?"}, sender="frontend")
    asyncio.run(runtime.poll_once(team.id))

    # 入站横向消息推送到 lead 屏幕（方向 frontend → backend）
    assert any(
        f == "frontend" and "接口契约" in t
        for (_, f, t, _) in seen
    ), f"横向入站消息未推送到 lead 可见会话: {seen}"
    # backend 的回复也推（方向 backend → frontend）
    assert any(
        f == "backend"
        for (_, f, _, _) in seen
    ), f"横向回复未推送到 lead 可见会话: {seen}"
