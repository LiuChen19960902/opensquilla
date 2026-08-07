"""P1: real-LLM teammate executor.

Each wake-up (one mailbox message) becomes a real agent turn in the
teammate's *own* session via ``TaskRuntime.enqueue`` — an independent
context window per member (ability #2). Turns are submitted asynchronously
(fire-and-forget from the poll loop) and completed turns are collected on
later polls, so one slow teammate never blocks the rest of the team: the
resident loop stays responsive while LLM turns run in the task pool.

Session layout
--------------
- session key: ``agent:main:teammate:<team_id>:<name>`` (per-member, durable)
- agent id:    ``teammate@<team_id>`` (distinct tool/context identity)

Turn protocol
-------------
``run_turn`` submits the turn and returns ``None`` (no synchronous reply).
``collect_completed`` polls the task ledger for finished turns, extracts the
assistant reply from the member's transcript, and returns ``CompletedTurn``
records — the runtime routes them (peer-to-peer) and emits idle notifications.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from opensquilla.engine.teammate import TeammateHandle, TeammateManager
from opensquilla.gateway.routing import build_subagent_route_envelope
from opensquilla.gateway.task_runtime import TaskQueueFullError
from opensquilla.session.models import AgentTaskStatus
from opensquilla.teammate.mailbox import MailboxMessage
from opensquilla.teammate.protocol import (
    NOTIFY_ONLY_TYPES,
    TYPE_PLAN_APPROVAL_RESPONSE,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_STEER,
    TYPE_TASK_ASSIGNMENT,
    FIELD_APPROVE,
    FIELD_FEEDBACK,
    FIELD_INSTRUCTION,
    FIELD_PLAN,
    FIELD_REQUEST_ID,
    FIELD_SUBJECT,
    FIELD_DESCRIPTION,
    FIELD_ASSIGNED_BY,
)

log = logging.getLogger("opensquilla.teammate.llm_executor")

# Sentinel returned by run_turn when the task queue is full (backpressure).
TURN_QUEUE_FULL = object()

TERMINAL_OK = frozenset({AgentTaskStatus.SUCCEEDED})
TERMINAL_BAD = frozenset(
    {AgentTaskStatus.FAILED, AgentTaskStatus.TIMEOUT, AgentTaskStatus.CANCELLED, AgentTaskStatus.ABANDONED}
)


@dataclass
class CompletedTurn:
    """One finished LLM turn, ready for the runtime to route."""

    handle: TeammateHandle
    reply: str
    reply_to: str
    error: str | None = None
    message: MailboxMessage | None = None


@dataclass
class _PendingTurn:
    handle: TeammateHandle
    message: MailboxMessage
    task_id: str
    submitted_at: float


def format_turn_text(message: MailboxMessage) -> str:
    """Render a mailbox message as the user-turn text for the member's LLM."""
    try:
        body = json.loads(message.text) if isinstance(message.text, str) else message.text
    except (ValueError, TypeError):
        body = {"text": message.text}
    if not isinstance(body, dict):
        body = {"text": str(body)}
    mtype = str(body.get("type", "message"))
    if mtype == TYPE_TASK_ASSIGNMENT:
        subject = body.get(FIELD_SUBJECT, "")
        description = body.get(FIELD_DESCRIPTION, "")
        by = body.get(FIELD_ASSIGNED_BY, "team-lead")
        return f"[task_assignment from {by}] {subject}\n{description}".strip()
    if mtype == "shutdown_request":
        return f"[shutdown_request] {body.get('reason', '')}".strip()
    if mtype == TYPE_STEER:
        instruction = body.get(FIELD_INSTRUCTION, "") or body.get("instruction", "")
        return f"[steer from {message.from_name}] {instruction}".strip()
    if mtype == TYPE_PLAN_APPROVAL_RESPONSE:
        approve = body.get(FIELD_APPROVE)
        feedback = body.get(FIELD_FEEDBACK, "")
        plan = body.get(FIELD_PLAN, "")
        verdict = "APPROVED" if approve else "REJECTED"
        text = f"[plan_approval_response from {message.from_name}] {verdict}"
        if plan:
            text += f"\nplan: {plan}"
        if feedback:
            text += f"\nfeedback: {feedback}"
        return text
    text = body.get("text", "")
    if isinstance(text, list):
        text = json.dumps(text, ensure_ascii=False)
    return f"[{mtype} from {message.from_name}] {text}".strip()


class TeammateLLMExecutor:
    """TaskRuntime-backed executor: one real agent turn per wake-up."""

    def __init__(
        self,
        task_runtime: Any,
        session_manager: Any,
        turn_timeout_s: float = 600.0,
    ) -> None:
        self.task_runtime = task_runtime
        self.session_manager = session_manager
        self.turn_timeout_s = turn_timeout_s
        self._pending: dict[str, list[_PendingTurn]] = {}

    # ── TeammateExecutor protocol ─────────────────────────────────────
    async def run_turn(
        self, manager: TeammateManager, handle: TeammateHandle, message: MailboxMessage
    ) -> str | None:
        """Submit one real LLM turn for ``handle``; returns None (async).

        Protocol-level messages are handled locally without an LLM turn:
        - ``shutdown_request`` → approve the handshake, reply synchronously
        - notify-only types (idle_notification, task_completed, ...) → no
          reply slot, no LLM turn (courtesy-loop guard, same as the stub)
        """
        body = _decode_message_body(message)
        mtype = str(body.get("type", "message"))
        if mtype == TYPE_SHUTDOWN_REQUEST:
            request_id = str(body.get(FIELD_REQUEST_ID, ""))
            manager.approve_shutdown(handle.team_id, handle.name, request_id)
            return f"shutdown approved ({request_id})"
        if mtype in NOTIFY_ONLY_TYPES:
            return None

        try:
            _session, created = await self.session_manager.get_or_create(
                handle.session_key, agent_id=handle.agent_id
            )
        except Exception as exc:  # defensive: never break the poll loop
            log.warning("teammate.llm_session_failed", run_id=handle.run_id, exc_info=True)
            raise RuntimeError(f"session create failed: {exc}") from exc

        if created:
            await self.session_manager.append_message(
                handle.session_key,
                "system",
                _member_system_prompt(manager, handle),
                provenance={"kind": "teammate_bootstrap"},
            )

        text = format_turn_text(message)
        await self.session_manager.append_message(
            handle.session_key,
            "user",
            text,
            provenance={"kind": "teammate_turn", "message_type": message.type},
        )

        team = manager.registry.get_team(handle.team_id)
        parent_session_key = (team.lead_session_key if team is not None else None) or ""
        envelope = build_subagent_route_envelope(
            session_key=handle.session_key,
            parent_session_key=parent_session_key,
            agent_id=handle.agent_id,
            run_id=uuid.uuid4().hex,
            origin="teammate",
        )
        try:
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
            _PendingTurn(
                handle=handle,
                message=message,
                task_id=task.task_id,
                submitted_at=time.monotonic(),
            )
        )
        return None

    # ── collection (poll-loop side) ───────────────────────────────────
    async def collect_completed(
        self, manager: TeammateManager, team_id: str
    ) -> list[CompletedTurn]:
        """Return finished turns for this team; drop stale/errored ones."""
        done: list[CompletedTurn] = []
        for run_id, pending_list in list(self._pending.items()):
            if not pending_list:
                continue
            handle = pending_list[0].handle
            if handle.team_id != team_id:
                continue
            # Process queue head-first: only the oldest pending for this handle
            # is checked each poll; later items wait for head to complete,
            # preserving order and preventing overwrite loss (卡死 fix).
            pending = pending_list[0]
            try:
                record = await self.task_runtime.status(pending.task_id)
            except Exception:
                continue  # not persisted yet / unknown — retry next poll
            status = record.status

            if status in TERMINAL_OK:
                reply = await self._extract_reply(handle.session_key)
                pending_list.pop(0)
                if not pending_list:
                    self._pending.pop(run_id, None)
                reply_to = getattr(pending.message, "from_name", None) or "team-lead"
                done.append(
                    CompletedTurn(
                        handle=handle,
                        reply=reply,
                        reply_to=reply_to,
                        message=pending.message,
                    )
                )
            elif status in TERMINAL_BAD:
                pending_list.pop(0)
                if not pending_list:
                    self._pending.pop(run_id, None)
                reason = record.terminal_reason or record.error_message or status.value
                done.append(
                    CompletedTurn(
                        handle=handle,
                        reply="",
                        reply_to=getattr(pending.message, "from_name", None) or "team-lead",
                        error=f"turn {status.value}: {reason}",
                    )
                )
            # else: still queued/running — keep polling
        return done

    async def _extract_reply(self, session_key: str) -> str:
        """Last assistant text in the member's transcript."""
        try:
            transcript = await self.session_manager.get_transcript(session_key)
        except Exception:
            return "(no reply)"
        for entry in reversed(transcript):
            role = getattr(entry, "role", None)
            if isinstance(entry, dict):
                role = entry.get("role")
            if role != "assistant":
                continue
            content = getattr(entry, "content", "") or ""
            if isinstance(entry, dict):
                content = entry.get("content") or ""
            if isinstance(content, str) and content.strip():
                return content.strip()
        return "(no reply)"

    # ── teardown ──────────────────────────────────────────────────────
    async def cancel_all(self) -> None:
        self._pending.clear()

    def has_pending(self, handle: TeammateHandle) -> bool:
        """True while an async turn for ``handle`` is still in flight."""
        pending = self._pending.get(handle.run_id)
        return bool(pending)


def _member_system_prompt(manager: TeammateManager, handle: TeammateHandle) -> str:
    """Bootstrap system prompt for a teammate's own session.

    Tells the member who it is, what role it plays (from the spawn prompt),
    how to reach the lead and peers, and that it is a resident worker whose
    mail arrives as user turns (one wake-up = one turn). Messages it sends
    via ``teammate_send`` are real peer-to-peer communication.
    """
    team = manager.registry.get_team(handle.team_id)
    role = getattr(handle, "prompt", "") or ""
    peers = [m.name for m in team.members if m.name != handle.name] if team else []
    peer_list = ", ".join(peers) if peers else "(none yet)"
    return (
        f"You are {handle.name}, a teammate on team '{team.name if team else handle.team_id}' "
        f"(team_id: {handle.team_id}). Your role: {role}\n"
        f"Your teammates: {peer_list}. The team lead is 'team-lead'.\n"
        "You are a resident worker: each message you receive is a task or "
        "question, and you reply with your result. You keep working across "
        "many turns — your session persists.\n"
        "To communicate, call teammate_send with team_id='"
        f"{handle.team_id}" "', sender='" f"{handle.name}" "' and set 'to' to a "
        "teammate name or 'team-lead'. Example: "
        "teammate_send(team_id='" f"{handle.team_id}" "', to='alice', "
        "sender='" f"{handle.name}" "', message={'type':'message','text':'...'}). "
        "Use teammate_task_list / teammate_task_update to track work on the "
        "shared task board. When you need the lead to approve a plan or "
        "unblock you, send a plan_approval_request message to 'team-lead' and "
        "wait for the response.\n"
        "Keep replies concise and factual."
    ).strip()


def _decode_message_body(message: MailboxMessage) -> dict[str, Any]:
    text = getattr(message, "text", None) or ""
    try:
        decoded = json.loads(text) if isinstance(text, str) else text
    except (ValueError, TypeError):
        return {"text": text, "type": "message"}
    return decoded if isinstance(decoded, dict) else {"text": str(decoded), "type": "message"}
