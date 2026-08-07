"""Teammate runtime — the resident event loop.

Each teammate is a *resident* worker: it polls its mailbox, processes
messages (one wake-up = one turn), and returns to idle. This is the structural
difference from a one-shot subagent: the teammate keeps its identity, mailbox,
and session across many turns.

P0 ships ``StubTeammateExecutor`` (a plain handler function) so the
spawn → send → process → reply → shutdown loop is fully testable without a
model. P1 replaces the stub with ``TaskRuntime.enqueue`` so each wake-up runs
a real agent turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("opensquilla.teammate.runtime")

from opensquilla.engine.teammate import TeammateHandle, TeammateManager, team_dir_of
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    NOTIFY_ONLY_TYPES,
    TYPE_IDLE_NOTIFICATION,
    TYPE_MESSAGE,
    TYPE_PLAN_APPROVAL_REQUEST,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_TASK_ASSIGNMENT,
    FIELD_IDLE_REASON,
    FIELD_REQUEST_ID,
    FIELD_SUMMARY,
    FIELD_TASK_ID,
)

# Handler: (manager, handle, message_body) -> reply text (or None).
TeammateHandler = Callable[[TeammateManager, TeammateHandle, dict[str, Any]], Awaitable[str | None]]

# Visible sink: stream a teammate reply onto the team lead's visible session
# (Claude Code-style shared transcript).
# (team_id, from_name, text, notify_only) -> None
#
# ``notify_only=True`` → system-role notice: shown on the lead's screen but
# excluded from the lead's LLM context (Claude Code's idle_notification
# semantics — the lead is not woken and does not auto-reply).
# ``notify_only=False`` → user-role message: enters the lead's context and can
# wake a turn (Claude Code's SendMessage semantics; reserved for P1 when a
# teammate needs a real decision from the lead).
VisibleSink = Callable[[str, str, str, bool], Awaitable[None]]


class StubTeammateExecutor:
    """Executor that routes each message to a Python handler (no LLM).

    The default handler replies with a summary and answers shutdown requests;
    tests may inject a custom handler to simulate arbitrary teammate behavior.
    """

    def __init__(self, handler: TeammateHandler | None = None):
        self._handler = handler or _default_handler

    async def run_turn(
        self, manager: TeammateManager, handle: TeammateHandle, message: Any
    ) -> str:
        body = _decode_body(message)
        reply = await self._handler(manager, handle, body)
        return reply or ""

    # ── async-turn protocol (stub is always synchronous) ───────────────
    async def collect_completed(self, manager: TeammateManager, team_id: str) -> list[Any]:
        """Stub never has in-flight turns."""
        return []

    def has_pending(self, handle: TeammateHandle) -> bool:
        return False


async def _default_handler(
    manager: TeammateManager, handle: TeammateHandle, body: dict[str, Any]
) -> str | None:
    """Default stub behavior: acknowledge, answer shutdown, ignore rest.

    Notify-only message types (``idle_notification``, ``task_completed``,
    heartbeats, acks, ...) return ``None`` — no reply, no second hop. This is
    the protocol-level courtesy-loop guard: information messages carry no
    response slot, exactly like Claude Code's idle notification.
    """
    msg_type = body.get("type", "message")
    if msg_type == TYPE_SHUTDOWN_REQUEST:
        request_id = str(body.get(FIELD_REQUEST_ID, ""))
        manager.approve_shutdown(handle.team_id, handle.name, request_id)
        return f"shutdown approved ({request_id})"
    if msg_type in NOTIFY_ONLY_TYPES:
        return None
    subject = body.get("subject") or body.get("description") or json.dumps(body, ensure_ascii=False)
    return f"ack [{msg_type}] {subject}"


def _decode_body(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    text = getattr(message, "text", None) or str(message)
    try:
        decoded = json.loads(text)
        return decoded if isinstance(decoded, dict) else {"text": text}
    except (json.JSONDecodeError, TypeError):
        return {"text": text}


class TeammateRuntime:
    """Resident loop that polls every registered teammate's mailbox.

    P0 exposes ``poll_once`` (drive one poll cycle) and ``run`` (continuous
    loop) so tests and demos can use either synchronous stepping or a
    background task.
    """

    def __init__(
        self,
        manager: TeammateManager,
        poll_interval: float = 0.25,
        executor: Any = None,
        shutdown_timeout: float | None = 30.0,
        idle_timeout: float | None = None,
        visible_sink: VisibleSink | None = None,
        completion_sink: Any | None = None,
    ):
        self.manager = manager
        self.poll_interval = poll_interval
        # Event-driven wake-up: send_message() can nudge the loop immediately
        # instead of waiting for the next poll_interval tick.
        self._wake: asyncio.Event | None = None
        # Executor protocol (duck-typed):
        #   run_turn(manager, handle, message) -> str | None
        #       None = async turn submitted (collected later via collect_completed)
        #   collect_completed(manager, team_id) -> list[CompletedTurn]
        #   has_pending(handle) -> bool
        # Both StubTeammateExecutor (sync ack) and TeammateLLMExecutor (P1,
        # real agent turn via TaskRuntime.enqueue) implement this protocol.
        self.executor = executor or StubTeammateExecutor()
        self.shutdown_timeout = shutdown_timeout
        self.idle_timeout = idle_timeout
        self.visible_sink = visible_sink
        # Completion callback ``(team_id, member_name, summary, reason)`` —
        # fired on every turn-completion idle notification (in addition to
        # the mailbox write), so the gateway can surface member completions
        # to the lead's LLM context and wake the lead (pi-subagents-style
        # task notification). Optional; fire-and-forget, never awaited inline.
        self.completion_sink = completion_sink
        self._running = False
        # Wire event-driven wake so manager.send_message() can nudge the loop.
        try:
            self.manager._on_message = self.notify  # type: ignore[attr-defined]
        except Exception:
            pass

    # ── reply routing (peer-to-peer, Claude Code SendMessage) ──────────
    async def _push_visible(
        self, team_id: str, from_label: str, text: str, notify_only: bool = True
    ) -> None:
        """Stream a message onto the lead's visible session (Claude Code-style
        shared transcript). ``notify_only`` keeps it out of the lead's LLM
        context (system role — visible, no wake-up)."""
        if self.visible_sink is None:
            return
        try:
            await self.visible_sink(team_id, from_label, text, notify_only)
        except Exception:
            log.warning(
                "teammate.visible_sink_failed",
                team_id=team_id,
                exc_info=True,
            )

    async def _route_reply(
        self,
        team_id: str,
        handle: TeammateHandle,
        reply: str,
        reply_to: str,
        source_message: Any = None,
    ) -> None:
        """Route one reply to the original sender's inbox.

        The reply goes to whoever sent the message that triggered the turn:
        ``team-lead`` for lead-assigned tasks, or a peer teammate for lateral
        communication. Every reply is ALSO streamed onto the lead's visible
        session (shared transcript) with the direction ``handle → reply_to``,
        so the lead sees the whole teammate conversation in real time —
        Claude Code's shared-transcript behavior. Escalations
        (``plan_approval_request``) additionally enter the lead's LLM context
        (user role) so the lead is prompted to make the decision; everything
        else is a notification (system role, visible but no wake-up).
        """
        reply_inbox = Mailbox.open(team_dir_of(self.manager.registry, team_id), reply_to)
        reply_inbox.send(handle.name, {"type": TYPE_MESSAGE, "text": reply})
        if self.visible_sink is not None:
            # Escalation: a plan_approval_request expects a real lead decision,
            # so it must enter the lead's LLM context (user role). Everything
            # else is a notification (system role, visible but no wake-up).
            escalate = False
            source_body = _decode_body(source_message) if source_message is not None else {}
            if isinstance(source_body, dict):
                escalate = source_body.get("type") == TYPE_PLAN_APPROVAL_REQUEST
            await self._push_visible(
                team_id, handle.name, reply, not escalate
            )

    # ── guardrails (Claude Code-inspired) ──────────────────────────────
    def _fire_completion_sink(
        self, team_id: str, member_name: str, summary: str, reason: str
    ) -> None:
        """Fire the completion callback without blocking the poll loop.

        Fire-and-forget: the sink is best-effort (the gateway surfaces the
        completion to the lead's context/wake path) and must never stall
        the runtime, so failures are logged and swallowed.
        """
        if self.completion_sink is None:
            return
        try:
            asyncio.get_running_loop().create_task(
                self.completion_sink(team_id, member_name, summary, reason)
            )
        except Exception:
            log.warning("teammate.completion_sink_failed", exc_info=True)

    def _notify_idle(
        self, handle: TeammateHandle, team_id: str, summary: str, reason: str = "available"
    ) -> None:
        """Turn completed → emit a system-level ``idle_notification`` to the lead.

        Mirrors Claude Code's Stop-hook behavior: the runtime (not the model)
        tells the lead "this teammate is free, here's what it did". No LLM
        turn is consumed and no reply is possible — it is a notification.
        """
        lead_inbox = Mailbox.open(team_dir_of(self.manager.registry, team_id), "team-lead")
        lead_inbox.send(
            handle.name,
            {
                "type": TYPE_IDLE_NOTIFICATION,
                FIELD_IDLE_REASON: reason,
                FIELD_SUMMARY: summary,
            },
        )
        self._fire_completion_sink(team_id, handle.name, summary, reason)

    def _hit_max_turns(self, handle: TeammateHandle, team_id: str) -> None:
        """Teammate exceeded its turn budget: error out (``max_turns`` stop)."""
        handle.error = f"Reached max turns ({handle.max_turns})"
        handle.stop_reason = "max_turns"
        self.manager.handles.update_status(handle.run_id, "error")
        self.manager.registry.update_member_status(team_id, handle.name, "error")
        lead_inbox = Mailbox.open(team_dir_of(self.manager.registry, team_id), "team-lead")
        lead_inbox.send(
            handle.name,
            {
                "type": TYPE_IDLE_NOTIFICATION,
                FIELD_IDLE_REASON: "max_turns_reached",
                FIELD_SUMMARY: handle.error,
            },
        )
        self._fire_completion_sink(team_id, handle.name, handle.error, "max_turns_reached")

    def _enforce_shutdown_timeout(self, team_id: str) -> None:
        """Teammate stuck in ``shutting_down`` past the timeout → error.

        The shutdown handshake is cooperative: a teammate that never answers
        ``shutdown_request`` would otherwise hang forever. This is the
        last-resort channel (Claude Code's lead-side forced termination).
        """
        if self.shutdown_timeout is None:
            return
        now = time.monotonic()
        for handle in self.manager.handles.list(team_id):
            if handle.status != "shutting_down" or handle.shutdown_requested_at is None:
                continue
            if now - handle.shutdown_requested_at > self.shutdown_timeout:
                handle.error = f"shutdown timed out after {self.shutdown_timeout:.1f}s"
                handle.stop_reason = "shutdown_timeout"
                self.manager.handles.update_status(handle.run_id, "error")
                self.manager.registry.update_member_status(team_id, handle.name, "error")

    def _enforce_idle_timeout(self, team_id: str) -> None:
        """Auto-converge: force-shutdown teammates idle past the timeout.

        Completion of a task leaves a teammate idle; if nothing wakes it
        within ``idle_timeout``, the team converges on its own instead of
        holding resident processes forever.
        """
        for handle in self.manager.idle_since(team_id, self.idle_timeout or 0):
            self.manager.force_shutdown(team_id, handle.name, reason="idle_timeout")
            lead_inbox = Mailbox.open(team_dir_of(self.manager.registry, team_id), "team-lead")
            lead_inbox.send(
                handle.name,
                {
                    "type": TYPE_IDLE_NOTIFICATION,
                    FIELD_IDLE_REASON: "idle_timeout",
                    FIELD_SUMMARY: f"idle >{self.idle_timeout:.1f}s, auto-shutdown",
                },
            )
            self._fire_completion_sink(
                team_id,
                handle.name,
                f"idle >{self.idle_timeout:.1f}s, auto-shutdown",
                "idle_timeout",
            )

    async def poll_once(self, team_id: str) -> list[str]:
        """One poll cycle: drain unread mail for every live teammate.

        Returns the list of replies produced this cycle.
        """
        replies: list[str] = []
        # P1: collect finished async LLM turns (submitted on earlier polls by
        # an async executor like TeammateLLMExecutor) and route their replies.
        collector = getattr(self.executor, "collect_completed", None)
        if collector is not None:
            try:
                completed = await collector(self.manager, team_id)
            except Exception:
                log.warning(
                    "teammate.collect_failed",
                    team_id=team_id,
                    exc_info=True,
                )
                completed = []
            for turn in completed:
                if turn.error:
                    # Turn failed at the runtime level (timeout/cancel/error).
                    # Report to the lead as an idle notification — no reply.
                    replies.append(f"{turn.handle.name}: ERROR {turn.error}")
                    self.manager.handles.update_status(turn.handle.run_id, "idle")
                    self._notify_idle(
                        turn.handle, team_id, summary=f"turn failed: {turn.error}", reason="turn_failed"
                    )
                    continue
                if turn.reply:
                    replies.append(f"{turn.handle.name}: {turn.reply}")
                    await self._route_reply(
                        team_id,
                        turn.handle,
                        turn.reply,
                        turn.reply_to,
                        source_message=turn.message,
                    )
                    self.manager.handles.update_status(turn.handle.run_id, "idle")
                    self._notify_idle(turn.handle, team_id, summary=turn.reply)

        for handle in self.manager.handles.list(team_id):
            if handle.status in ("done", "error", "aborted"):
                continue
            mailbox: Mailbox = handle.mailbox
            messages = mailbox.read_unread()
            if not messages:
                continue
            self.manager.handles.update_status(handle.run_id, "running")
            turn_summary: list[str] = []
            # Track which messages were successfully submitted to executor
            # so we can requeue only the failed tail on exception (Bug3 fix).
            processed_count = 0
            try:
                for message in messages:
                    # ── turn budget (Claude Code ``max_turns``) ─────────
                    handle.turn_count += 1
                    if handle.max_turns is not None and handle.turn_count > handle.max_turns:
                        self._hit_max_turns(handle, team_id)
                        break
                    # Stream inbound LATERAL traffic (a teammate talking to
                    # this member) onto the lead's visible session so the
                    # whole conversation is visible in real time — Claude
                    # Code shared-transcript behavior. Lead-originated tasks
                    # and notify-only system chatter are not re-streamed.
                    sender = getattr(message, "from_name", None)
                    if (
                        sender
                        and sender != "team-lead"
                        and self.visible_sink is not None
                    ):
                        body = _decode_body(message)
                        if body.get("type") not in NOTIFY_ONLY_TYPES:
                            summary = (
                                body.get("subject")
                                or body.get("description")
                                or body.get("text")
                                or ""
                            )
                            if isinstance(summary, list):
                                summary = json.dumps(summary, ensure_ascii=False)
                            await self._push_visible(
                                team_id,
                                sender,
                                str(summary)[:400],
                                True,
                            )
                    # Bug2 fix: task_assignment arrival auto-claims taskboard
                    # so a teammate that acks without calling teammate_task_update
                    # still moves task from pending -> in_progress.
                    try:
                        body = _decode_body(message)
                        if body.get("type") == TYPE_TASK_ASSIGNMENT:
                            task_id = body.get(FIELD_TASK_ID) or body.get("taskId")
                            if task_id:
                                from opensquilla.teammate.taskboard import TaskBoard

                                tb = TaskBoard(team_dir_of(self.manager.registry, team_id) / "tasks.json")
                                try:
                                    tb.claim(str(task_id), assignee=handle.name)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    reply = await self.executor.run_turn(self.manager, handle, message)
                    if reply is TURN_QUEUE_FULL:
                        # Backpressure: session pending queue full. Coalesce the failed
                        # and trailing messages into one queued envelope (pi-subagents
                        # style: one turn per session, [QUEUED TEAM MESSAGE] separated).
                        # The teammate stays alive; the next poll retries the merged
                        # message as a single turn.
                        try:
                            mailbox.merge_messages(messages[processed_count:])
                        except Exception:
                            pass
                        break
                    if reply:
                        replies.append(f"{handle.name}: {reply}")
                        turn_summary.append(reply)
                        # Route the reply back to the ORIGINAL sender's inbox
                        # (peer-to-peer). When the sender was team-lead this is
                        # the lead inbox; when a teammate sent it, the reply
                        # goes to that teammate — lateral communication.
                        reply_to = getattr(message, "from_name", None) or "team-lead"
                        await self._route_reply(
                            team_id, handle, reply, reply_to, source_message=message
                        )
                    processed_count += 1
            except Exception as exc:  # pragma: no cover - defensive
                # Bug3 fix: messages already marked read before try; requeue
                # the failed tail so it is not permanently lost.
                try:
                    failed = messages[processed_count:]
                    if failed:
                        mailbox.requeue(failed)
                except Exception:
                    pass
                handle.error = str(exc)
                handle.stop_reason = "handler_error"
                self.manager.handles.update_status(handle.run_id, "error")
                replies.append(f"{handle.name}: ERROR {exc}")
            else:
                if handle.status in ("done", "error", "aborted"):
                    continue
                # A teammate mid-shutdown must stay in the handshake flow
                # (awaiting approval or timeout) — never drift back to idle.
                if handle.shutdown_requested_at is not None:
                    self.manager.handles.update_status(handle.run_id, "shutting_down")
                    continue
                # P1: an async turn is still in flight — stay "running", do
                # NOT go idle or emit an idle notification yet. The finished
                # turn is collected on a later poll.
                has_pending = getattr(self.executor, "has_pending", None)
                if callable(has_pending) and has_pending(handle):
                    self.manager.handles.update_status(handle.run_id, "running")
                    continue
                self.manager.handles.update_status(handle.run_id, "idle")
                # ── Stop-hook semantics: report availability to the lead ──
                self._notify_idle(
                    handle,
                    team_id,
                    summary="; ".join(turn_summary) or f"processed {len(messages)} message(s)",
                )
        # ── shutdown handshake timeout ──────────────────────────────────
        self._enforce_shutdown_timeout(team_id)
        return replies

    def notify(self) -> None:
        """Wake the poll loop immediately (called after send_message)."""
        if self._wake is not None and not self._wake.is_set():
            self._wake.set()

    async def _sleep_or_wake(self, stop: asyncio.Event | None) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        else:
            self._wake.clear()
        # Wait for either the interval or an explicit notify(), plus stop signal.
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.poll_interval
            )
        except asyncio.TimeoutError:
            pass
        if stop is not None and stop.is_set():
            return

    async def run(
        self, team_id: str, stop: asyncio.Event | None = None
    ) -> None:
        """Continuous poll loop until ``stop`` is set or no handles remain.

        With ``idle_timeout`` configured on the runtime, teams with no
        pending work converge automatically: idle teammates are
        force-shutdown and the loop exits.
        """
        self._running = True
        self._wake = asyncio.Event()
        try:
            while not (stop is not None and stop.is_set()):
                await self.poll_once(team_id)
                live = [
                    h for h in self.manager.handles.list(team_id)
                    if h.status not in ("done", "error", "aborted")
                ]
                if not live:
                    break
                if self.idle_timeout is not None:
                    self._enforce_idle_timeout(team_id)
                    live = [
                        h for h in self.manager.handles.list(team_id)
                        if h.status not in ("done", "error", "aborted")
                    ]
                    if not live:
                        break
                await self._sleep_or_wake(stop)
        finally:
            self._running = False
            self._wake = None

    async def run_all(self, stop: asyncio.Event | None = None) -> None:
        """Resident loop over ALL teams (gateway mode).

        Unlike ``run`` (single team, exits when that team converges), this
        loop keeps polling every team in the registry until ``stop`` is set.
        Per-team failures are isolated so one bad team never kills the loop.
        """
        self._running = True
        self._wake = asyncio.Event()
        try:
            while not (stop is not None and stop.is_set()):
                teams = self.manager.registry.list_teams()
                for team in teams:
                    try:
                        await self.poll_once(team.id)
                        if self.idle_timeout is not None:
                            self._enforce_idle_timeout(team.id)
                    except Exception:  # pragma: no cover - defensive
                        pass
                await self._sleep_or_wake(stop)
        finally:
            self._running = False
            self._wake = None
