"""Team registry — persisted team definitions and member identity.

Teams live under ``state_dir("teams")/<team_id>/`` with a ``config.json``
mirroring the Claude Code team config shape (name / createdAt / leadAgentId /
members[]). The registry is the source of truth for "who is on which team"
and is used by the tools layer and the runtime.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from opensquilla.paths import state_dir
from opensquilla.teammate.models import TeamConfig, TeammateInfo


def default_teams_root() -> Path:
    """Canonical teams root: ``<home>/state/teams`` (profile-aware)."""
    return state_dir("teams")


class TeamRegistry:
    """Load/save team configs under a root directory.

    Thread-safe in-process; cross-process safety relies on atomic writes.
    """

    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else default_teams_root()
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── team lifecycle ─────────────────────────────────────────────────
    def create_team(
        self,
        name: str,
        lead_agent_id: str,
        lead_session_key: str = "",
    ) -> TeamConfig:
        team_id = uuid.uuid4().hex
        config = TeamConfig(
            name=name,
            id=team_id,
            lead_agent_id=lead_agent_id,
            lead_session_key=lead_session_key,
        )
        with self._lock:
            self._save(config)
        return config

    def get_team(self, team_id: str) -> TeamConfig | None:
        with self._lock:
            return self._load(team_id)

    def list_teams(self) -> list[TeamConfig]:
        with self._lock:
            configs: list[TeamConfig] = []
            if not self.root.exists():
                return configs
            for child in sorted(self.root.iterdir()):
                if child.is_dir() and (child / "config.json").exists():
                    cfg = self._load(child.name)
                    if cfg is not None:
                        configs.append(cfg)
            return configs

    def delete_team(self, team_id: str) -> bool:
        """Remove a team directory. Fails (returns False) if the team still
        has active members — mirroring Claude Code's TeamDelete guard."""
        with self._lock:
            config = self._load(team_id)
            if config is None:
                return False
            active = [m for m in config.members if m.status not in ("done", "aborted", "error")]
            if active:
                return False
            import shutil

            shutil.rmtree(self._team_dir(team_id), ignore_errors=True)
            return True

    # ── member lifecycle ───────────────────────────────────────────────
    def add_member(
        self,
        team_id: str,
        name: str,
        *,
        agent_type: str = "general-purpose",
        model: str | None = None,
        prompt: str = "",
        color: str = "",
        max_turns: int | None = None,
    ) -> TeammateInfo:
        with self._lock:
            config = self._load(team_id)
            if config is None:
                raise KeyError(f"no such team: {team_id}")
            if config.member(name) is not None:
                raise ValueError(f"member '{name}' already on team {team_id}")
            info = TeammateInfo(
                agent_id=f"{name}@{team_id}",
                name=name,
                agent_type=agent_type,
                model=model,
                prompt=prompt,
                color=color or _color_for(len(config.members)),
                joined_at=_now_ms(),
                max_turns=max_turns,
            )
            config.members.append(info)
            self._save(config)
        return info

    def get_member(self, team_id: str, name: str) -> TeammateInfo | None:
        config = self.get_team(team_id)
        return config.member(name) if config else None

    def update_member_status(self, team_id: str, name: str, status: str) -> None:
        with self._lock:
            config = self._load(team_id)
            if config is None:
                return
            member = config.member(name)
            if member is None:
                return
            member.status = status
            self._save(config)

    # ── internals ──────────────────────────────────────────────────────
    def _team_dir(self, team_id: str) -> Path:
        return self.root / team_id

    def _config_path(self, team_id: str) -> Path:
        return self._team_dir(team_id) / "config.json"

    def _save(self, config: TeamConfig) -> None:
        team_dir = self._team_dir(config.id)
        team_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
        tmp = team_dir / "config.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, team_dir / "config.json")

    def _load(self, team_id: str) -> TeamConfig | None:
        path = self._config_path(team_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(raw, dict):
            return None
        return TeamConfig.from_dict(raw)


_COLORS = ("blue", "green", "yellow", "magenta", "cyan", "red", "white")


def _color_for(index: int) -> str:
    return _COLORS[index % len(_COLORS)]


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
