"""Teammate handles, registry, and manager — resident team members.

This is the engine-side counterpart of ``engine/subagent.py`` but for
*resident* teammates: a teammate keeps its session, mailbox, and identity
across many turns instead of being archived after one task.

Lifecycle states:
    spawning → running ⇄ idle → shutting_down → done
                    ↘ error / aborted

P0 scope: identity + mailbox + status management, driven by a caller-supplied
executor (see ``opensquilla.teammate.runtime``). The TaskRuntime.enqueue
execution integration is P1.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from opensquilla.session.keys import build_teammate_session_key
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    TYPE_SHUTDOWN_APPROVED,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_SHUTDOWN_REJECTED,
    FIELD_REQUEST_ID,
    FIELD_REASON,
)

if TYPE_CHECKING:
    from opensquilla.teammate.registry import TeamRegistry


class TeammateExecutor(Protocol):
    """Executes one teammate wake-up (a message processed as one turn).

    P0 ships ``StubTeammateExecutor`` (a plain handler function); P1 wires
    this to ``TaskRuntime.enqueue`` so a teammate runs as a real agent turn.
    """

    async def run_turn(self, handle: "TeammateHandle", message: Any) -> str:
        """Process ``message`` and return the teammate's reply text."""
        ...


@dataclass
class TeammateHandle:
    """Reference to one resident teammate (mirrors SubagentHandle)."""

    run_id: str
    team_id: str
    name: str
    agent_id: str  # "<name>@<team_id>"
    session_key: str
    team_dir: Path
    status: str = "spawning"  # spawning|running|idle|shutting_down|done|error|aborted
    model: str | None = None
    prompt: str = ""
    task: asyncio.Task[Any] | None = None
    spawned_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    last_activity: float = field(default_factory=time.monotonic)
    error: str = ""
    # ── guardrails (Claude Code-inspired) ─────────────────────────────
    max_turns: int | None = None  # hard turn budget; None = unlimited
    turn_count: int = 0  # turns consumed so far (one per processed message)
    shutdown_requested_at: float | None = None  # set when shutting_down
    stop_reason: str = ""  # max_turns | shutdown_timeout | killed | idle_timeout | ...

    @property
    def mailbox(self) -> Mailbox:
        return Mailbox.open(self.team_dir, self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "team_id": self.team_id,
            "name": self.name,
            "agent_id": self.agent_id,
            "session_key": self.session_key,
            "status": self.status,
            "model": self.model,
            "max_turns": self.max_turns,
            "turn_count": self.turn_count,
            "stop_reason": self.stop_reason,
            "idle_seconds": round(time.monotonic() - self.last_activity, 1),
        }


class TeammateRegistry:
    """In-memory registry of resident teammate handles (per process)."""

    def __init__(self) -> None:
        self._runs: dict[str, TeammateHandle] = {}

    def register(self, handle: TeammateHandle) -> None:
        self._runs[handle.run_id] = handle

    def get(self, run_id: str) -> TeammateHandle | None:
        return self._runs.get(run_id)

    def get_by_agent_id(self, team_id: str, name: str) -> TeammateHandle | None:
        for handle in self._runs.values():
            if handle.team_id == team_id and handle.name == name:
                return handle
        return None

    def list(self, team_id: str | None = None) -> list[TeammateHandle]:
        handles = list(self._runs.values())
        if team_id is not None:
            handles = [h for h in handles if h.team_id == team_id]
        return sorted(handles, key=lambda h: h.name)

    def update_status(self, run_id: str, status: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None:
            return False
        handle.status = status
        handle.last_activity = time.monotonic()
        if status in ("done", "error", "aborted"):
            handle.completed_at = time.monotonic()
        return True

    def count_active(self) -> int:
        return sum(1 for h in self._runs.values() if h.status not in ("done", "error", "aborted"))

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for handle in self._runs.values():
            counts[handle.status] = counts.get(handle.status, 0) + 1
        return counts


class TeammateManager:
    """High-level API for spawning, messaging, and shutting down teammates.

    P0 uses a caller-supplied executor (stub in tests / demo). The registry
    provides durable team identity; mailboxes provide durable messaging.
    """

    def __init__(
        self,
        registry: "TeamRegistry",
        executor: TeammateExecutor | None = None,
    ):
        self.registry = registry
        self.handles = TeammateRegistry()
        self.executor = executor
        # Optional wake hook set by TeammateRuntime to get event-driven polling.
        self._on_message: Any = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def spawn_teammate(
        self,
        team_id: str,
        name: str,
        prompt: str,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> TeammateHandle:
        """Register a new resident teammate and create its mailbox.

        ``max_turns`` is the Claude Code ``maxTurns`` guard: a hard budget on
        total processed messages. After the budget is exceeded the teammate
        errors out (``stop_reason="max_turns"``) instead of looping forever.
        """
        if not name.strip():
            raise ValueError("teammate name is required")
        if not prompt.strip():
            raise ValueError("teammate prompt is required")
        info = self.registry.add_member(
            team_id, name, model=model, prompt=prompt, max_turns=max_turns
        )
        team = self.registry.get_team(team_id)
        assert team is not None
        run_id = uuid.uuid4().hex
        handle = TeammateHandle(
            run_id=run_id,
            team_id=team_id,
            name=name,
            agent_id=info.agent_id,
            session_key=build_teammate_session_key("main", team_id, name),
            team_dir=team_dir_of(self.registry, team_id),
            status="idle",
            model=model,
            prompt=prompt,
            max_turns=max_turns,
        )
        handle.mailbox.ensure()
        self.handles.register(handle)
        self.registry.update_member_status(team_id, name, "idle")
        return handle

    # ── messaging ──────────────────────────────────────────────────────
    def send_message(self, team_id: str, to_name: str, body: dict[str, Any], sender: str = "team-lead") -> Any:
        """Write a typed message to a teammate's inbox."""
        handle = self.handles.get_by_agent_id(team_id, to_name)
        if handle is None:
            if to_name == "team-lead":
                # The lead is not a registered team member; write directly to
                # its persisted inbox (same special case as approve_shutdown).
                inbox = Mailbox.open(team_dir_of(self.registry, team_id), to_name)
            else:
                team = self.registry.get_team(team_id)
                if team is None or team.member(to_name) is None:
                    raise KeyError(f"no teammate '{to_name}' on team {team_id}")
                # Fall back to a persisted-only mailbox (no live handle).
                inbox = Mailbox.open(team_dir_of(self.registry, team_id), to_name)
        else:
            inbox = handle.mailbox
        result = inbox.send(sender, body)
        # Event-driven wake: nudge the runtime without waiting for poll_interval.
        try:
            if self._on_message is not None:
                self._on_message()
        except Exception:
            pass
        return result

    def request_shutdown(self, team_id: str, name: str, reason: str = "") -> str:
        """Send a shutdown request and mark the teammate shutting_down."""
        request_id = f"shutdown-{uuid.uuid4().hex}@{name}"
        body: dict[str, Any] = {
            "type": TYPE_SHUTDOWN_REQUEST,
            FIELD_REQUEST_ID: request_id,
        }
        if reason:
            body[FIELD_REASON] = reason
        self.send_message(team_id, name, body)
        handle = self.handles.get_by_agent_id(team_id, name)
        if handle is not None:
            handle.shutdown_requested_at = time.monotonic()
            self.handles.update_status(handle.run_id, "shutting_down")
            self.registry.update_member_status(team_id, name, "shutting_down")
        return request_id

    def force_shutdown(self, team_id: str, name: str, reason: str = "") -> None:
        """Kill a teammate without the shutdown handshake (Claude Code ``kill``).

        The teammate transitions straight to ``aborted`` — a terminal state —
        even if it never approved (or even saw) a shutdown request. Used as
        the last-resort channel when a teammate is stuck in ``shutting_down``
        or has been idle too long.
        """
        handle = self.handles.get_by_agent_id(team_id, name)
        if handle is not None:
            handle.stop_reason = reason or "killed"
            self.handles.update_status(handle.run_id, "aborted")
        self.registry.update_member_status(team_id, name, "aborted")

    def approve_shutdown(self, team_id: str, name: str, request_id: str) -> None:
        """Teammate agrees to shut down: notify the lead's inbox, mark done.

        Mirrors Claude Code's shutdown handshake: lead sends
        ``shutdown_request`` → teammate replies ``shutdown_approved`` to the
        lead's inbox and transitions to ``done``.
        """
        lead_inbox = Mailbox.open(team_dir_of(self.registry, team_id), "team-lead")
        lead_inbox.send(
            name,
            {
                "type": TYPE_SHUTDOWN_APPROVED,
                FIELD_REQUEST_ID: request_id,
                "from": name,
            },
        )
        handle = self.handles.get_by_agent_id(team_id, name)
        if handle is not None:
            self.handles.update_status(handle.run_id, "done")
        self.registry.update_member_status(team_id, name, "done")

    # ── status ─────────────────────────────────────────────────────────
    def restore_teams(self) -> int:
        """Recreate in-memory handles for persisted team members.

        Handles live in memory only; the registry (config.json) is durable.
        After a gateway restart, members still exist on disk but have no
        live handle — this method rebuilds them so the resident loop can
        poll their mailboxes again. Members already in a terminal status
        (done/error/aborted) stay terminal. Returns the number of restored
        (non-terminal) handles.
        """
        restored = 0
        for team in self.registry.list_teams():
            for member in team.members:
                if self.handles.get_by_agent_id(team.id, member.name) is not None:
                    continue
                if member.status in ("done", "error", "aborted"):
                    continue
                handle = TeammateHandle(
                    run_id=uuid.uuid4().hex,
                    team_id=team.id,
                    name=member.name,
                    agent_id=member.agent_id,
                    session_key=build_teammate_session_key("main", team.id, member.name),
                    team_dir=team_dir_of(self.registry, team.id),
                    status="idle",
                    model=member.model,
                    prompt=member.prompt,
                    max_turns=member.max_turns,
                )
                handle.mailbox.ensure()
                self.handles.register(handle)
                restored += 1
        return restored

    def status(self, team_id: str) -> dict[str, Any]:
        handles = self.handles.list(team_id)
        return {
            "team_id": team_id,
            "members": [h.to_dict() for h in handles],
            "summary": self.handles.summary(),
        }

    def idle_since(self, team_id: str, threshold_seconds: float) -> list[TeammateHandle]:
        """Teammates idle for at least ``threshold_seconds``.

        Gives the lead (or an auto-convergence policy) the data to decide
        whether to wake, terminate, or force-shutdown a quiet teammate —
        the same decision surface Claude Code's ``TeammateIdle`` hook offers.
        """
        now = time.monotonic()
        return [
            h
            for h in self.handles.list(team_id)
            if h.status == "idle" and now - h.last_activity >= threshold_seconds
        ]


def team_dir_of(registry: "TeamRegistry", team_id: str) -> Path:
    """Resolve the on-disk team directory for a team id."""
    return registry.root / team_id
