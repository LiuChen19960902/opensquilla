"""Tests for the P1 real-LLM teammate executor.

TeammateLLMExecutor turns each mailbox wake-up into a real agent turn in the
teammate's own session via TaskRuntime.enqueue, then collects finished turns
on later polls. These tests use a fake task runtime so no provider/LLM is
needed — they verify the protocol decisions (async submit, collect, reply
extraction, shutdown handshake, notify-only skip).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.teammate import TeammateManager
from opensquilla.teammate.llm_executor import (
    CompletedTurn,
    TeammateLLMExecutor,
    format_turn_text,
)
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    TYPE_IDLE_NOTIFICATION,
    TYPE_MESSAGE,
    TYPE_PLAN_APPROVAL_REQUEST,
    TYPE_PLAN_APPROVAL_RESPONSE,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_TASK_ASSIGNMENT,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import TeammateRuntime
from opensquilla.session.models import AgentTaskStatus


# ── fakes ──────────────────────────────────────────────────────────────


class FakeTaskRecord:
    def __init__(self, task_id: str, status: AgentTaskStatus, terminal_reason: str | None = None):
        self.task_id = task_id
        self.status = status
        self.terminal_reason = terminal_reason
        self.error_message = None


class FakeTaskRuntime:
    """Minimal TaskRuntime stand-in: records enqueues, resolves statuses."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []  # (session_key, text)
        self._statuses: dict[str, AgentTaskStatus] = {}
        self._ids: dict[str, str] = {}

    def enqueue(self, envelope: Any, message: str, **kwargs: Any) -> Any:
        return self._enqueue_impl(envelope, message)

    async def enqueue(self, envelope: Any, message: str, **kwargs: Any) -> Any:
        return self._enqueue_impl(envelope, message)

    def _enqueue_impl(self, envelope: Any, message: str) -> Any:
        task_id = f"task-{len(self.enqueued) + 1}"
        self.enqueued.append((envelope.session_key, message))
        self._ids[envelope.session_key] = task_id
        self._statuses[task_id] = AgentTaskStatus.RUNNING
        return type("TaskHandle", (), {"task_id": task_id})()

    async def status(self, task_id: str) -> FakeTaskRecord:
        status = self._statuses.get(task_id, AgentTaskStatus.QUEUED)
        return FakeTaskRecord(task_id, status)

    def finish(self, session_key: str, status: AgentTaskStatus = AgentTaskStatus.SUCCEEDED) -> None:
        self._statuses[self._ids[session_key]] = status


class FakeSessionManager:
    """In-memory transcript per session key."""

    def __init__(self) -> None:
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.created: set[str] = set()

    async def get_or_create(self, session_key: str, agent_id: str | None = None) -> tuple[Any, bool]:
        existed = session_key in self.sessions
        self.created.add(session_key)
        return self.sessions.setdefault(session_key, []), not existed

    async def append_message(self, session_key: str, role: str, content: str, **kwargs: Any) -> None:
        self.sessions.setdefault(session_key, []).append({"role": role, "content": content})

    async def get_transcript(self, session_key: str) -> list[dict[str, Any]]:
        return list(self.sessions.get(session_key, []))


# ── fixtures ───────────────────────────────────────────────────────────


def _make_world(tmp_path: Path) -> tuple[TeammateManager, FakeTaskRuntime, FakeSessionManager, str]:
    registry = TeamRegistry(tmp_path / "teams")
    manager = TeammateManager(registry)
    team = manager.registry.create_team("team-a", lead_agent_id="main", lead_session_key="lead-session")
    manager.spawn_teammate(team.id, "alice", "you are alice")
    return manager, FakeTaskRuntime(), FakeSessionManager(), team.id


def _inbox(manager: TeammateManager, team_id: str, name: str) -> Mailbox:
    from opensquilla.engine.teammate import team_dir_of

    return Mailbox.open(team_dir_of(manager.registry, team_id), name)


def _assign(manager: TeammateManager, team_id: str, to: str, subject: str, description: str = "") -> None:
    manager.send_message(
        team_id,
        to,
        {"type": TYPE_TASK_ASSIGNMENT, "subject": subject, "description": description},
    )


# ── format_turn_text ───────────────────────────────────────────────────


def test_format_turn_text_task_assignment(tmp_path: Path) -> None:
    manager, _, _, team_id = _make_world(tmp_path)
    msg = _inbox(manager, team_id, "alice").send(
        "team-lead",
        {"type": TYPE_TASK_ASSIGNMENT, "subject": "写文档", "description": "详细点", "assignedBy": "team-lead"},
    )
    text = format_turn_text(msg)
    assert "[task_assignment from team-lead]" in text
    assert "写文档" in text and "详细点" in text


# ── async submit + collect ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_executor_submits_turn_and_collects_reply(tmp_path: Path) -> None:
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    assert handle is not None
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)

    _assign(manager, team_id, "alice", "算 1+1")
    msg = _inbox(manager, team_id, "alice").read_unread()[0]

    # Submit: returns None (async), session created, task enqueued.
    result = await executor.run_turn(manager, handle, msg)
    assert result is None
    assert handle.session_key in sessions.created
    assert runtime.enqueued and runtime.enqueued[0][0] == handle.session_key

    # Still running → collect returns nothing.
    assert await executor.collect_completed(manager, team_id) == []

    # Finish → transcript has an assistant reply → collected.
    sessions.sessions[handle.session_key].append({"role": "assistant", "content": "答案是 2"})
    runtime.finish(handle.session_key)
    done = await executor.collect_completed(manager, team_id)
    assert len(done) == 1
    assert isinstance(done[0], CompletedTurn)
    assert done[0].reply == "答案是 2"
    assert done[0].reply_to == "team-lead"
    assert not executor.has_pending(handle)


@pytest.mark.asyncio
async def test_llm_executor_collects_failed_turn_with_error(tmp_path: Path) -> None:
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)
    _assign(manager, team_id, "alice", "任务")
    msg = _inbox(manager, team_id, "alice").read_unread()[0]

    await executor.run_turn(manager, handle, msg)
    runtime.finish(handle.session_key, AgentTaskStatus.TIMEOUT)
    done = await executor.collect_completed(manager, team_id)
    assert len(done) == 1
    assert done[0].reply == ""
    assert done[0].error is not None and "timeout" in done[0].error


@pytest.mark.asyncio
async def test_llm_executor_handles_shutdown_request_locally(tmp_path: Path) -> None:
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)

    msg = _inbox(manager, team_id, "alice").send(
        "team-lead",
        {"type": TYPE_SHUTDOWN_REQUEST, "request_id": "shut-1@alice"},
    )
    result = await executor.run_turn(manager, handle, msg)
    assert result == "shutdown approved (shut-1@alice)"
    # No LLM turn was submitted.
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_llm_executor_skips_notify_only(tmp_path: Path) -> None:
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)

    msg = _inbox(manager, team_id, "alice").send(
        "bob",
        {"type": TYPE_IDLE_NOTIFICATION, "idleReason": "available", "summary": "done"},
    )
    result = await executor.run_turn(manager, handle, msg)
    assert result is None
    assert runtime.enqueued == []


# ── end-to-end through the runtime loop ────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_routes_collected_reply_to_sender(tmp_path: Path) -> None:
    """poll_once routes a collected LLM reply back to the sender's inbox."""
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)
    tr = TeammateRuntime(manager, executor=executor, poll_interval=0.01)

    _assign(manager, team_id, "alice", "任务")
    await tr.poll_once(team_id=team_id)  # submit (async)

    sessions.sessions[handle.session_key].append({"role": "assistant", "content": "完成"})
    runtime.finish(handle.session_key)
    replies = await tr.poll_once(team_id=team_id)  # collect + route

    assert replies == ["alice: 完成"]
    lead = _inbox(manager, team_id, "team-lead")
    unread = lead.read_unread()
    # The reply goes to the lead inbox as a plain message (not idle_notification).
    assert any(m.from_name == "alice" and "完成" in m.text for m in unread)
    # Handle returns to idle.
    assert manager.handles.get_by_agent_id(team_id, "alice").status == "idle"


@pytest.mark.asyncio
async def test_runtime_keeps_running_while_turn_in_flight(tmp_path: Path) -> None:
    manager, runtime, sessions, team_id = _make_world(tmp_path)
    handle = manager.handles.get_by_agent_id(team_id, "alice")
    executor = TeammateLLMExecutor(task_runtime=runtime, session_manager=sessions)
    tr = TeammateRuntime(manager, executor=executor, poll_interval=0.01)

    _assign(manager, team_id, "alice", "长任务")
    await tr.poll_once(team_id=team_id)

    # Turn still running → second poll must NOT flip to idle.
    await tr.poll_once(team_id=team_id)
    assert manager.handles.get_by_agent_id(team_id, "alice").status == "running"

    sessions.sessions[handle.session_key].append({"role": "assistant", "content": "done"})
    runtime.finish(handle.session_key)
    await tr.poll_once(team_id=team_id)
    assert manager.handles.get_by_agent_id(team_id, "alice").status == "idle"


@pytest.mark.asyncio
async def test_llm_executor_escalation_wakes_lead(tmp_path: Path) -> None:
    """plan_approval_request 路由到 lead 时 notify_only=False（进 lead LLM 上下文）。

    能力 6 escalation：成员需要 lead 决策时发 plan_approval_request，lead 会话
    收到 user 角色消息（不是 system 通知），真正唤醒 lead 决策。
    """
    from opensquilla.teammate.llm_executor import TeammateLLMExecutor

    sink_calls: list[tuple[str, str, bool]] = []
    async def fake_sink(team_id: str, from_name: str, text: str, notify_only: bool) -> None:
        sink_calls.append((from_name, text, notify_only))

    registry = TeamRegistry(tmp_path / "teams_esc")
    manager = TeammateManager(registry)
    team = manager.registry.create_team("esc", lead_agent_id="main", lead_session_key="lead-session")
    manager.spawn_teammate(team.id, "alice", "you are alice")
    handle = manager.handles.get_by_agent_id(team.id, "alice")
    assert handle is not None

    sessions = FakeSessionManager()
    runtime_fake = FakeTaskRuntime()
    executor = TeammateLLMExecutor(task_runtime=runtime_fake, session_manager=sessions)

    # 成员 → lead 的 plan_approval_request
    manager.send_message(
        team.id, "alice",
        {"type": TYPE_PLAN_APPROVAL_REQUEST, "subject": "需要批准部署方案", "plan": "blue-green"},
        sender="team-lead",
    )
    msg = _inbox(manager, team.id, "alice").read_unread()[0]
    await executor.run_turn(manager, handle, msg)  # 提交异步回合

    # 成员回复"请求批准"
    sessions.sessions[handle.session_key].append({"role": "assistant", "content": "请求 lead 批准：blue-green 部署"})
    runtime_fake.finish(handle.session_key)

    runtime = TeammateRuntime(manager, poll_interval=0.01, executor=executor, visible_sink=fake_sink)
    replies = await runtime.poll_once(team.id)
    assert any("请求 lead 批准" in r for r in replies)
    # escalation：notify_only=False → 进 lead 上下文唤醒决策
    assert sink_calls, "visible_sink 未被调用"
    from_name, text, notify_only = sink_calls[0]
    assert from_name == "alice"
    assert notify_only is False, "plan_approval_request 必须唤醒 lead（user 角色）"

    # lead 决策 → plan_approval_response → 成员继续干活
    manager.send_message(
        team.id, "alice",
        {"type": TYPE_PLAN_APPROVAL_RESPONSE, "approve": True, "feedback": "可以，注意回滚"},
        sender="team-lead",
    )
    msg2 = _inbox(manager, team.id, "alice").read_unread()[0]
    await executor.run_turn(manager, handle, msg2)
    assert any(sk == handle.session_key for sk in sessions.created)
