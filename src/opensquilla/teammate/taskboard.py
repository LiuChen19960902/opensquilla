"""Shared task board for a team.

A team's work is coordinated through a task list (like Claude Code's
TaskCreate/TaskList/TaskUpdate and agno's ``_task_tools.py``). Tasks are
created ``pending`` with no owner; idle teammates claim them with
``claim()`` and report completion with ``complete()``.

Persistence is a single ``tasks.json`` per team directory.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from opensquilla.teammate.models import _now_ms


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Task:
    """A single unit of team work with owner/status/result tracking."""

    def __init__(
        self,
        *,
        title: str,
        description: str = "",
        task_id: str | None = None,
        status: TaskStatus | str = TaskStatus.PENDING,
        assignee: str | None = None,
        created_by: str = "",
        dependencies: list[str] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        completed_at: str | None = None,
        output: str = "",
    ):
        self.id = task_id or uuid.uuid4().hex[:8]
        self.title = title
        self.description = description
        self.status = TaskStatus(status) if isinstance(status, str) else status
        self.assignee = assignee
        self.created_by = created_by
        self.dependencies = list(dependencies or [])
        now = _now_iso()
        self.created_at = created_at or now
        self.updated_at = updated_at or now
        self.completed_at = completed_at
        self.output = output

    # ── JSON round-trip ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "assignee": self.assignee,
            "createdBy": self.created_by,
            "dependencies": self.dependencies,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "completedAt": self.completed_at,
            "output": self.output,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        return cls(
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            task_id=str(raw.get("id", "")),
            status=TaskStatus(str(raw.get("status", "pending"))),
            assignee=raw.get("assignee"),
            created_by=str(raw.get("createdBy", "")),
            dependencies=[str(d) for d in raw.get("dependencies", [])],
            created_at=raw.get("createdAt"),
            updated_at=raw.get("updatedAt"),
            completed_at=raw.get("completedAt"),
            output=str(raw.get("output", "")),
        )


class TaskBoard:
    """Persisted shared task board for one team."""

    def __init__(self, path: Path | None = None):
        self.path = path  # tasks.json path; None = in-memory only
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        if path is not None:
            self._load()

    # ── persistence ────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, list):
            self._tasks = {t.id: t for t in (Task.from_dict(i) for i in raw if isinstance(i, dict))}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [t.to_dict() for t in self._tasks.values()], ensure_ascii=False, indent=2
        )
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    # ── mutations ──────────────────────────────────────────────────────
    def create(
        self,
        title: str,
        description: str = "",
        created_by: str = "",
        assignee: str | None = None,
        dependencies: list[str] | None = None,
    ) -> Task:
        """Create a task. Defaults to ``pending`` with no owner."""
        with self._lock:
            task = Task(
                title=title,
                description=description,
                created_by=created_by,
                assignee=assignee,
                dependencies=dependencies,
            )
            self._tasks[task.id] = task
            self._save()
        return task

    def claim(self, task_id: str, assignee: str) -> Task:
        """Idle teammate claims an unassigned pending task.

        Raises ValueError when the task is already owned or not pending.
        """
        with self._lock:
            task = self._get_locked(task_id)
            if task.status is not TaskStatus.PENDING:
                raise ValueError(f"task {task_id} is {task.status.value}, cannot claim")
            if task.assignee and task.assignee != assignee:
                raise ValueError(f"task {task_id} already assigned to {task.assignee}")
            task.assignee = assignee
            task.status = TaskStatus.IN_PROGRESS
            task.updated_at = _now_iso()
            self._save()
        return task

    def complete(self, task_id: str, output: str = "") -> Task:
        with self._lock:
            task = self._get_locked(task_id)
            task.status = TaskStatus.COMPLETED
            task.output = output
            task.completed_at = _now_iso()
            task.updated_at = task.completed_at
            self._save()
        return task

    def fail(self, task_id: str, error: str = "") -> Task:
        with self._lock:
            task = self._get_locked(task_id)
            task.status = TaskStatus.FAILED
            task.output = error
            task.completed_at = _now_iso()
            task.updated_at = task.completed_at
            self._save()
        return task

    def block(self, task_id: str) -> Task:
        with self._lock:
            task = self._get_locked(task_id)
            task.status = TaskStatus.BLOCKED
            task.updated_at = _now_iso()
            self._save()
        return task

    # ── reads ──────────────────────────────────────────────────────────
    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task

    def _get_locked(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return task

    def list(
        self,
        status: TaskStatus | str | None = None,
        assignee: str | None = None,
    ) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        wanted = TaskStatus(status) if isinstance(status, str) and status else status
        if wanted is not None:
            tasks = [t for t in tasks if t.status is wanted]
        if assignee is not None:
            tasks = [t for t in tasks if t.assignee == assignee]
        return sorted(tasks, key=lambda t: t.created_at)
