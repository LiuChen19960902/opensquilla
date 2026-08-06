"""Teammate anti-runaway guardrails (Claude Code-inspired).

Validates the four protective layers ported from Claude Code's teammate
runtime:

1. notify-only message types never produce a reply (no protocol seat for
   small talk) — kills the courtesy-loop structurally.
2. completing a turn emits a system-level ``idle_notification`` to the lead
   (Stop-hook semantics), so the lead always knows who is free.
3. shutdown has a second channel: ``force_shutdown`` (kill) plus a timeout
   that errors a teammate stuck in ``shutting_down``.
4. ``max_turns`` bounds every teammate's total turns; a courtesy loop
   between two teammates cannot run forever.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    FIELD_IDLE_REASON,
    FIELD_SUMMARY,
    TYPE_IDLE_NOTIFICATION,
    TYPE_TASK_ASSIGNMENT,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import StubTeammateExecutor, TeammateRuntime


def _make_world(
    tmp_path: Path, handler=None, **rt_kwargs
) -> tuple[TeammateManager, TeammateRuntime, object]:
    registry = TeamRegistry(tmp_path)
    executor = StubTeammateExecutor(handler)
    manager = TeammateManager(registry, executor=executor)
    runtime = TeammateRuntime(manager, poll_interval=0.01, executor=executor, **rt_kwargs)
    return manager, runtime, executor


def _team(manager: TeammateManager):
    return manager.registry.create_team(name="demo", lead_agent_id="team-lead")


def _lead_inbox(manager: TeammateManager, team_id: str) -> Mailbox:
    return Mailbox.open(team_dir_of(manager.registry, team_id), "team-lead")


# ── layer 1: notify-only messages carry no response slot ──────────────


def test_notify_only_messages_do_not_produce_replies(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "researcher", "you research")

    for msg_type in (
        "task_claimed",
        "task_completed",
        "task_failed",
        "idle_notification",
        "heartbeat",
        "ack",
    ):
        manager.send_message(team.id, "researcher", {"type": msg_type, "text": "hi"})

    replies = asyncio.run(runtime.poll_once(team.id))
    assert replies == [], "notify-only messages must not produce replies"
    assert handle.status == "idle"

    lead = _lead_inbox(manager, team.id)
    routed = [m for m in lead.peek() if '"text"' in m.text and '"type": "message"' in m.text]
    assert routed == [], "no second hop may be routed for notify-only traffic"


def test_default_handler_returns_none_for_notify_only(tmp_path: Path) -> None:
    from opensquilla.teammate.runtime import _default_handler

    manager, _, _ = _make_world(tmp_path)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "worker", "you work")

    async def probe() -> None:
        for msg_type in (
            "task_completed",
            "task_failed",
            "idle_notification",
            "heartbeat",
            "ack",
        ):
            reply = await _default_handler(manager, handle, {"type": msg_type, "text": "x"})
            assert reply is None, f"{msg_type} must not produce a reply"

    asyncio.run(probe())


# ── layer 2: turn completion emits idle_notification (Stop-hook) ───────


def test_idle_notification_reports_turn_to_lead(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "researcher", "you research")

    manager.send_message(
        team.id, "researcher", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "dig"}
    )
    asyncio.run(runtime.poll_once(team.id))

    lead = _lead_inbox(manager, team.id)
    idle = [m for m in lead.peek() if TYPE_IDLE_NOTIFICATION in m.text]
    assert idle, "turn completion must emit idle_notification"
    body = json.loads(idle[-1].text)
    assert body[FIELD_IDLE_REASON] == "available"
    assert "ack" in body[FIELD_SUMMARY]
    assert idle[-1].from_name == "researcher"


def test_no_idle_notification_without_turn(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)
    team = _team(manager)
    manager.spawn_teammate(team.id, "worker", "you work")

    asyncio.run(runtime.poll_once(team.id))  # nothing in the mailbox
    lead = _lead_inbox(manager, team.id)
    assert lead.count_unread() == 0, "no turn, no idle notification"


# ── layer 3: force_shutdown (kill) + shutdown timeout ─────────────────


def test_force_shutdown_kills_without_handshake(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "worker", "you work")

    manager.force_shutdown(team.id, "worker", reason="lead aborted")
    assert handle.status == "aborted"
    assert handle.stop_reason == "lead aborted"
    assert manager.handles.count_active() == 0


def test_shutdown_timeout_errors_stuck_teammate(tmp_path: Path) -> None:
    async def stubborn(manager, handle, body):
        return None  # never approves the shutdown request

    manager, runtime, _ = _make_world(tmp_path, handler=stubborn, shutdown_timeout=0.05)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "worker", "you work")

    manager.request_shutdown(team.id, "worker", reason="go away")
    assert handle.status == "shutting_down"

    asyncio.run(runtime.poll_once(team.id))  # processes request; no approval
    assert handle.status == "shutting_down"

    asyncio.run(asyncio.sleep(0.06))
    asyncio.run(runtime.poll_once(team.id))  # timeout check fires
    assert handle.status == "error"
    assert handle.stop_reason == "shutdown_timeout"


# ── layer 4: max_turns bounds every teammate ──────────────────────────


def test_max_turns_errors_teammate(tmp_path: Path) -> None:
    manager, runtime, _ = _make_world(tmp_path)
    team = _team(manager)
    handle = manager.spawn_teammate(team.id, "worker", "you work", max_turns=2)

    for i in range(3):
        manager.send_message(
            team.id, "worker", {"type": TYPE_TASK_ASSIGNMENT, "taskId": str(i), "subject": f"t{i}"}
        )

    asyncio.run(runtime.poll_once(team.id))
    assert handle.status == "error"
    assert handle.stop_reason == "max_turns"
    assert "max turns" in handle.error.lower()
    assert handle.turn_count == 3  # budget checked on the 3rd message

    # the lead was notified with the stop reason
    lead = _lead_inbox(manager, team.id)
    notice = [m for m in lead.peek() if "max_turns_reached" in m.text]
    assert notice


# ── integration: a courtesy loop cannot run forever ───────────────────


def test_courtesy_loop_is_bounded_by_guardrails(tmp_path: Path) -> None:
    """Two teammates exchanging pleasantries cannot run forever.

    max_turns errors the chatterbox; idle_timeout then reaps the quiet peer
    and the team converges on its own.
    """

    async def polite(manager, handle, body):
        peer = "peer" if handle.name == "worker" else "worker"
        # reply AND forward the pleasantry (the runaway pattern)
        manager.send_message(
            handle.team_id, peer, {"type": "message", "text": "thanks!"}, sender=handle.name
        )
        return "you're welcome"

    manager, runtime, _ = _make_world(tmp_path, handler=polite, idle_timeout=0.05)
    team = _team(manager)
    worker = manager.spawn_teammate(team.id, "worker", "you work", max_turns=4)
    peer = manager.spawn_teammate(team.id, "peer", "you help", max_turns=4)

    manager.send_message(
        team.id, "worker", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "start"}
    )
    asyncio.run(runtime.run(team.id))

    assert manager.handles.count_active() == 0, "team must converge"
    assert worker.status in ("error", "aborted")
    assert peer.status in ("error", "aborted")
    assert worker.stop_reason in ("max_turns", "idle_timeout")
    assert peer.stop_reason in ("max_turns", "idle_timeout")
