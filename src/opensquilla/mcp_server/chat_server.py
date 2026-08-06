"""Chat-only inbound MCP server.

Exposes a single ``chat`` tool that drives the *full* OpenSquilla agent on the
gateway: when no ``session_id`` is given the tool creates a fresh session;
when one is supplied it continues that session (so callers can hold a session
id across turns). It then waits for the agent's terminal event and returns
the final assistant reply. All agent capability (tools, skills, model routing,
permissions) stays on the gateway — the exposed surface is just a chat
conversation.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from opensquilla.gateway_client import GatewayRPCClient, normalize_gateway_url
from opensquilla.mcp_server.bridge import _TERMINAL_EVENTS, _normalize_event_frame


_SUBAGENT_ALLOWED_HINT = (
    "\n\n[execution mode] You MAY use subagents to parallelize this task "
    "(up to 10 concurrent). Decide yourself whether the task benefits from "
    "parallel subtask execution."
)
_SUBAGENT_FORBIDDEN_HINT = (
    "\n\n[execution mode] You MUST NOT use subagents for this task. "
    "Complete all steps serially yourself."
)


def _compose_message(message: str, use_subagents: bool) -> str:
    """Append the subagent execution-mode directive to the caller's message."""
    hint = _SUBAGENT_ALLOWED_HINT if use_subagents else _SUBAGENT_FORBIDDEN_HINT
    return message + hint


def _extract_final_reply(history: Any) -> str:
    """Return the last non-empty assistant text from a session history payload."""
    messages = history.get("messages", []) if isinstance(history, dict) else []
    for row in reversed(messages):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            return text
        tool_calls = row.get("tool_calls")
        if isinstance(tool_calls, list):
            for segment in reversed(tool_calls):
                if isinstance(segment, dict) and segment.get("type") == "text":
                    segment_text = segment.get("text")
                    if isinstance(segment_text, str) and segment_text.strip():
                        return segment_text
    return ""


class OpenSquillaChatBridge:
    """One-shot chat flow over gateway RPCs: create-or-continue, wait, reply."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        client_factory: Callable[[], GatewayRPCClient] = GatewayRPCClient,
    ) -> None:
        raw_url = (
            gateway_url
            or os.environ.get("OPENSQUILLA_GATEWAY_URL")
            or "ws://localhost:18791/ws"
        )
        self.gateway_url = normalize_gateway_url(raw_url)
        self._client_factory = client_factory

    async def chat(
        self,
        session_id: str,
        message: str,
        *,
        timeout_ms: int = 300_000,
        use_subagents: bool = True,
    ) -> dict[str, Any]:
        client = self._client_factory()
        await client.connect(self.gateway_url)
        try:
            key = (session_id or "").strip()
            created = False
            if not key:
                created_resp = await client.call("sessions.create", {"agentId": "main"})
                key = str(created_resp.get("key") or created_resp.get("sessionId") or "")
                created = True

            await client.call(
                "sessions.messages.subscribe",
                {"key": key, "since_stream_seq": None},
            )

            await client.call(
                "sessions.send",
                {
                    "key": key,
                    "message": _compose_message(message, use_subagents),
                    "attachments": [],
                    "intent": "continue",
                    "_source": {
                        "caller_kind": "cli",
                        "channel_kind": "cli",
                        "channel_id": "mcp:chat",
                        "source_kind": "mcp",
                        "source_name": "mcp_chat_server",
                    },
                },
            )

            terminal_event: str | None = None
            terminal_payload: dict[str, Any] = {}
            deadline = time.monotonic() + max(1, timeout_ms) / 1000
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    frame = await client.recv_event(timeout=remaining)
                except TimeoutError:
                    break
                normalized = _normalize_event_frame(frame)
                payload = normalized.get("payload")
                if not isinstance(payload, dict) or payload.get("session_key") != key:
                    continue
                event_name = str(normalized.get("event") or "")
                if event_name in _TERMINAL_EVENTS:
                    terminal_event = event_name
                    terminal_payload = payload
                    break

            history = await client.session_history(key, limit=50)
            reply = _extract_final_reply(history)

            if terminal_event == "session.event.error":
                status = "error"
            elif terminal_event is None:
                status = "timeout"
            else:
                status = "ok"

            result: dict[str, Any] = {
                "session_id": key,
                "reply": reply,
                "status": status,
                "created": created,
            }
            if terminal_event is not None:
                result["terminal_event"] = terminal_event
            if status == "error":
                result["error"] = (
                    terminal_payload.get("message")
                    or terminal_payload.get("reason")
                    or terminal_payload.get("code")
                    or "agent turn failed"
                )
            return result
        finally:
            await client.close()


def create_chat_server(
    bridge: OpenSquillaChatBridge | None = None,
    *,
    name: str = "OpenSquillaChat",
    fastmcp_cls: type[Any] | None = None,
) -> Any:
    """Create a FastMCP app exposing a single chat tool backed by the full agent."""
    if fastmcp_cls is None:
        try:
            from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised through CLI behavior.
            raise RuntimeError(
                "The MCP server requires the optional dependency: install opensquilla[mcp]."
            ) from exc
        fastmcp_cls = FastMCP

    bridge = bridge or OpenSquillaChatBridge()
    mcp = fastmcp_cls(name, json_response=True)

    @mcp.tool(name="chat")
    async def chat(
        message: str,
        session_id: str = "",
        timeout_ms: int = 300_000,
        use_subagents: bool = True,
    ) -> dict[str, Any]:
        """Send a message to the full OpenSquilla agent and return its final reply.

        Args:
            message: The user message to send.
            session_id: Optional key of an existing session to continue. When
                empty, a new session is created and its id is returned.
            timeout_ms: Maximum time to wait for the agent to finish (default 5 min).
            use_subagents: Whether the agent may parallelize this task with
                subagents (up to 10 concurrent). Default True; pass False to
                force serial execution.
        """
        if not (message or "").strip():
            return {
                "session_id": session_id,
                "reply": "",
                "status": "error",
                "error": "message is required",
            }
        return await bridge.chat(
            session_id,
            message,
            timeout_ms=timeout_ms,
            use_subagents=use_subagents,
        )

    return mcp
