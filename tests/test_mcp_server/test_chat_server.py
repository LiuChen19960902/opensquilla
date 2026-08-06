from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opensquilla.mcp_server.chat_server import (
    OpenSquillaChatBridge,
    _compose_message,
    _extract_final_reply,
    create_chat_server,
)


class FakeGatewayClient:
    """Minimal GatewayRPCClient stand-in that records calls and replays events."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.connected_url: str | None = None
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in events or []:
            self.events.put_nowait(event)
        self.history_payload: dict[str, Any] = {
            "messages": [
                {"id": "m1", "role": "user", "text": "hello", "timestamp": 1},
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "final answer",
                    "timestamp": 2,
                },
            ]
        }
        self.create_key = "agent:main:new-session"

    async def connect(self, url: str) -> None:
        self.connected_url = url

    async def close(self) -> None:
        self.closed = True

    async def session_history(
        self, session_key: str, limit: int = 1000
    ) -> dict[str, Any]:
        self.calls.append(("sessions.history", {"key": session_key, "limit": limit}))
        return self.history_payload

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self.events.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError from None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "sessions.create":
            return {"key": self.create_key, "sessionId": self.create_key.rsplit(":", 1)[-1]}
        if method == "sessions.messages.subscribe":
            return {"current_stream_seq": 0, "replay_complete": True}
        if method == "sessions.send":
            return {"status": "accepted"}
        return {}


def _make_bridge(events: list[dict[str, Any]] | None = None) -> tuple[OpenSquillaChatBridge, FakeGatewayClient]:
    client = FakeGatewayClient(events)
    bridge = OpenSquillaChatBridge(
        gateway_url="ws://localhost:18791/ws",
        client_factory=lambda: client,
    )
    return bridge, client


def _terminal_done(key: str) -> dict[str, Any]:
    return {
        "event": "session.event.done",
        "payload": {"session_key": key, "stream_seq": 5},
    }


def test_extract_final_reply_skips_empty_and_tool_only() -> None:
    history = {
        "messages": [
            {"id": "a", "role": "assistant", "text": "   ", "timestamp": 1},
            {"id": "b", "role": "assistant", "text": "", "timestamp": 2},
            {"id": "c", "role": "user", "text": "ignored", "timestamp": 3},
            {
                "id": "d",
                "role": "assistant",
                "text": "the real reply",
                "timestamp": 4,
            },
        ]
    }
    assert _extract_final_reply(history) == "the real reply"


def test_extract_final_reply_falls_back_to_tool_call_text_block() -> None:
    history = {
        "messages": [
            {
                "id": "m",
                "role": "assistant",
                "text": "",
                "tool_calls": [
                    {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": {}},
                    {"type": "text", "text": "reply inside tool_calls"},
                ],
            }
        ]
    }
    assert _extract_final_reply(history) == "reply inside tool_calls"


def test_chat_creates_session_when_no_session_id() -> None:
    bridge, client = _make_bridge([_terminal_done("agent:main:new-session")])
    result = asyncio.run(bridge.chat("", "hello there"))

    methods = [call[0] for call in client.calls]
    assert "sessions.create" in methods
    assert any(
        method == "sessions.send" and params and params.get("key") == "agent:main:new-session"
        for method, params in client.calls
    )
    assert result["session_id"] == "agent:main:new-session"
    assert result["created"] is True
    assert result["reply"] == "final answer"
    assert result["status"] == "ok"


def test_chat_continues_existing_session_without_create() -> None:
    bridge, client = _make_bridge([_terminal_done("agent:main:existing")])
    result = asyncio.run(bridge.chat("agent:main:existing", "continue this"))

    methods = [call[0] for call in client.calls]
    assert "sessions.create" not in methods
    assert any(
        method == "sessions.send" and params and params.get("key") == "agent:main:existing"
        for method, params in client.calls
    )
    assert result["created"] is False
    assert result["status"] == "ok"


def test_chat_error_event_returns_error_status() -> None:
    error_event = {
        "event": "session.event.error",
        "payload": {
            "session_key": "agent:main:new-session",
            "stream_seq": 6,
            "message": "agent exploded",
        },
    }
    bridge, _client = _make_bridge([error_event])
    result = asyncio.run(bridge.chat("", "boom"))

    assert result["status"] == "error"
    assert result["error"] == "agent exploded"
    assert result["terminal_event"] == "session.event.error"


def test_chat_timeout_when_no_terminal_event() -> None:
    bridge, _client = _make_bridge([])  # no terminal event ever arrives
    result = asyncio.run(bridge.chat("agent:main:busy", "slow turn", timeout_ms=50))

    assert result["status"] == "timeout"
    assert result.get("terminal_event") is None


def test_compose_message_allows_subagents_by_default() -> None:
    sent = _compose_message("do the thing", use_subagents=True)
    assert sent.startswith("do the thing")
    assert "MAY use subagents" in sent
    assert "10 concurrent" in sent
    assert "MUST NOT use subagents" not in sent


def test_compose_message_forbids_subagents_when_disabled() -> None:
    sent = _compose_message("do the thing", use_subagents=False)
    assert sent.startswith("do the thing")
    assert "MUST NOT use subagents" in sent
    assert "MAY use subagents" not in sent


def test_chat_send_forwards_use_subagents_hint() -> None:
    bridge, client = _make_bridge([_terminal_done("agent:main:new-session")])
    asyncio.run(bridge.chat("", "parallelize me", use_subagents=False))

    send_params = next(
        params for method, params in client.calls if method == "sessions.send"
    )
    sent_message = send_params["message"]
    assert sent_message.startswith("parallelize me")
    assert "MUST NOT use subagents" in sent_message


class FakeFastMCP:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None):
        def decorator(func: Any) -> Any:
            self.tools[name or func.__name__] = func
            return func

        return decorator

    def run(self, **kwargs: Any) -> None:  # pragma: no cover - CLI path
        raise AssertionError("run should not be invoked in unit tests")


class FakeChatBridge:
    async def chat(
        self,
        session_id: str,
        message: str,
        *,
        timeout_ms: int = 300_000,
        use_subagents: bool = True,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id or "agent:main:fake",
            "reply": f"echo:{message}",
            "status": "ok",
            "created": not session_id,
            "use_subagents": use_subagents,
        }


def test_create_chat_server_exposes_only_chat_tool() -> None:
    fake = FakeFastMCP("x")
    app = create_chat_server(FakeChatBridge(), fastmcp_cls=FakeFastMCP)
    # create_chat_server instantiates its own FakeFastMCP; inspect via the class
    assert app is not None
    assert set(app.tools.keys()) == {"chat"}


def test_create_chat_server_validates_empty_message() -> None:
    fake = FakeFastMCP("x")
    app = create_chat_server(FakeChatBridge(), fastmcp_cls=FakeFastMCP)
    result = asyncio.run(app.tools["chat"]("  ", "", 1000))
    assert result["status"] == "error"
    assert result["error"] == "message is required"
