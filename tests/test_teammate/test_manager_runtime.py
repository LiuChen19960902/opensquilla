"""Teammate resident-loop integration tests (stub executor).

Validates the full P0 loop with a plain Python handler standing in for an LLM:

    spawn → send(task_assignment) → poll (process) → reply in lead inbox
          → shutdown_request → teammate approves → done
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    TYPE_SHUTDOWN_APPROVED,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_TASK_ASSIGNMENT,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import StubTeammateExecutor, TeammateRuntime


def _make_world(tmp_path: Path, handler=None) -> tuple[TeammateManager, TeammateRuntime, object]:
    registry = TeamRegistry(tmp_path)
    executor = StubTeammateExecutor(handler)
    manager = TeammateManager(registry, executor=executor)
    runtime = TeammateRuntime(manager, poll_interval=0.01, executor=executor)
    return manager, runtime, executor


def test_full_resident_loop(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)

    team = manager.registry.create_team(name="demo", lead_agent_id="team-lead")
    handle = manager.spawn_teammate(
        team.id, "researcher", "you research things", model="test-model"
    )
    assert handle.status == "idle"
    assert manager.handles.count_active() == 1

    # 1. lead assigns a task
    manager.send_message(
        team.id,
        "researcher",
        {
            "type": TYPE_TASK_ASSIGNMENT,
            "taskId": "1",
            "subject": "dig into agno Team",
            "description": "read source, summarize",
        },
    )
    assert handle.mailbox.count_unread() == 1

    # 2. resident loop processes it (stub turn) and replies
    replies = asyncio.run(runtime.poll_once(team.id))
    assert replies, "expected a processed reply"
    assert "ack" in replies[0]
    assert handle.status == "idle"  # back to idle after processing
    assert handle.mailbox.count_unread() == 0

    # 3. teammate's reply lands in team-lead inbox (all-to-all routing)
    lead_inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "team-lead")
    lead_msgs = lead_inbox.peek()
    assert lead_msgs, "teammate should have replied to lead inbox"

    # 4. shutdown handshake: request → approve → done
    request_id = manager.request_shutdown(team.id, "researcher", reason="done for now")
    assert request_id.startswith("shutdown-")
    assert handle.status == "shutting_down"

    replies = asyncio.run(runtime.poll_once(team.id))
    assert any("shutdown approved" in r for r in replies)
    assert handle.status == "done"
    assert manager.handles.count_active() == 0

    # lead inbox saw the approval
    lead_msgs = lead_inbox.peek()
    approval = next(
        (m for m in lead_msgs if m.text and '"shutdown_approved"' in m.text), None
    )
    assert approval is not None, "shutdown_approved should be routed to lead inbox"


def test_task_claim_roundtrip(tmp_path: Path) -> None:
    """Idle teammate claims a pending task via the board."""
    manager, runtime, _ = _make_world(tmp_path)
    from opensquilla.teammate.taskboard import TaskBoard

    team = manager.registry.create_team(name="demo", lead_agent_id="team-lead")
    handle = manager.spawn_teammate(team.id, "worker", "you work")
    board = TaskBoard(team_dir_of(manager.registry, team.id) / "tasks.json")

    task = board.create(title="audit the diff", description="check changes", created_by="team-lead")
    assert task.status.value == "pending"
    assert task.assignee is None

    # worker claims it (as a teammate would via teammate_task_update)
    claimed = board.claim(task.id, handle.name)
    assert claimed.status.value == "in_progress"
    assert claimed.assignee == "worker"

    board.complete(task.id, "all checks pass")
    assert board.get(task.id).status.value == "completed"


def test_custom_handler_reply_routing(tmp_path: Path) -> None:
    """A teammate can send messages to *another* teammate (all-to-all)."""

    async def handler(manager, handle, body):
        if body.get("type") == TYPE_TASK_ASSIGNMENT:
            # teammate asks the peer for help — all-to-all via mailbox
            manager.send_message(handle.team_id, "peer", {"type": "message", "text": "help me"}, sender=handle.name)
            return "asking peer"
        return "ok"

    manager, runtime, _ = _make_world(tmp_path, handler=handler)
    team = manager.registry.create_team(name="demo", lead_agent_id="team-lead")
    worker = manager.spawn_teammate(team.id, "worker", "you work")
    manager.spawn_teammate(team.id, "peer", "you help")

    manager.send_message(team.id, "worker", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "9", "subject": "t"})
    asyncio.run(runtime.poll_once(team.id))

    peer_inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "peer")
    msgs = peer_inbox.peek()
    assert any(m.from_name == worker.name and "help me" in m.text for m in msgs)
