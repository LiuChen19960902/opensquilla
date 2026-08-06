"""Teammate data models — team configuration and mailbox message envelopes.

All models are plain dataclasses with explicit JSON round-trip helpers so the
subsystem stays dependency-free (mirrors the style of ``engine/subagent.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _now_ms() -> int:
    """Monotonic-friendly wall-clock milliseconds (UTC epoch)."""
    import time

    return int(time.time() * 1000)


@dataclass
class MailboxMessage:
    """A single message in a teammate's inbox file.

    Mirrors the Claude Code teammate mailbox envelope: ``from`` / ``text`` /
    ``timestamp`` / ``type`` / ``read``.
    """

    from_name: str
    text: str
    timestamp: str
    type: str = "message"
    read: bool = False

    # ── JSON round-trip ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_name,
            "text": self.text,
            "timestamp": self.timestamp,
            "type": self.type,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MailboxMessage":
        return cls(
            from_name=str(raw.get("from", "")),
            text=str(raw.get("text", "")),
            timestamp=str(raw.get("timestamp", "")),
            type=str(raw.get("type", "message")),
            read=bool(raw.get("read", False)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class TeammateInfo:
    """Identity of one resident team member (persisted in team config.json)."""

    agent_id: str  # canonical "<name>@<team_id>"
    name: str
    agent_type: str = "general-purpose"
    model: str | None = None
    prompt: str = ""
    color: str = ""
    joined_at: int = 0
    backend_type: str = "in-process"
    status: str = "spawning"  # spawning|running|idle|shutting_down|done|error|aborted
    max_turns: int | None = None  # Claude Code ``maxTurns``: hard turn budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "name": self.name,
            "agentType": self.agent_type,
            "model": self.model,
            "prompt": self.prompt,
            "color": self.color,
            "joinedAt": self.joined_at,
            "backendType": self.backend_type,
            "status": self.status,
            "maxTurns": self.max_turns,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeammateInfo":
        max_turns = raw.get("maxTurns")
        return cls(
            agent_id=str(raw.get("agentId", "")),
            name=str(raw.get("name", "")),
            agent_type=str(raw.get("agentType", "general-purpose")),
            model=raw.get("model"),
            prompt=str(raw.get("prompt", "")),
            color=str(raw.get("color", "")),
            joined_at=int(raw.get("joinedAt", 0) or 0),
            backend_type=str(raw.get("backendType", "in-process")),
            status=str(raw.get("status", "spawning")),
            max_turns=int(max_turns) if max_turns not in (None, "") else None,
        )


@dataclass
class TeamConfig:
    """Persisted team definition — mirrors the Claude Code team config.json."""

    name: str
    id: str  # hex uuid
    created_at: int = field(default_factory=_now_ms)
    lead_agent_id: str = ""
    lead_session_key: str = ""
    members: list[TeammateInfo] = field(default_factory=list)

    # ── JSON round-trip ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "createdAt": self.created_at,
            "leadAgentId": self.lead_agent_id,
            "leadSessionKey": self.lead_session_key,
            "members": [m.to_dict() for m in self.members],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeamConfig":
        members = [TeammateInfo.from_dict(m) for m in raw.get("members", [])]
        return cls(
            name=str(raw.get("name", "")),
            id=str(raw.get("id", "")),
            created_at=int(raw.get("createdAt", 0) or 0),
            lead_agent_id=str(raw.get("leadAgentId", "")),
            lead_session_key=str(raw.get("leadSessionKey", "")),
            members=members,
        )

    def member(self, name: str) -> TeammateInfo | None:
        for m in self.members:
            if m.name == name:
                return m
        return None
