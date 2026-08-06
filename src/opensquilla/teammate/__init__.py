"""OpenSquilla teammate subsystem — resident multi-agent collaboration.

A teammate is a *resident* worker (unlike a one-shot subagent): it keeps its
identity, mailbox, and session across many turns, can receive messages from
any team member, and can be shut down via a request/approval handshake.

P0 scope: team registry + file mailboxes + task board + tool registration +
a stub-driven resident runtime. The TaskRuntime.enqueue execution integration
(real agent turns) is P1.
"""

from __future__ import annotations

from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.models import MailboxMessage, TeamConfig, TeammateInfo
from opensquilla.teammate.protocol import (
    TYPE_MESSAGE,
    TYPE_PLAN_APPROVAL_REQUEST,
    TYPE_PLAN_APPROVAL_RESPONSE,
    TYPE_SHUTDOWN_APPROVED,
    TYPE_SHUTDOWN_REJECTED,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_TASK_ASSIGNMENT,
    TYPE_TASK_CLAIMED,
    TYPE_TASK_COMPLETED,
    TYPE_TASK_FAILED,
)
from opensquilla.teammate.registry import TeamRegistry, default_teams_root
from opensquilla.teammate.taskboard import Task, TaskBoard, TaskStatus

__all__ = [
    "Mailbox",
    "MailboxMessage",
    "TeamConfig",
    "TeammateInfo",
    "TeamRegistry",
    "default_teams_root",
    "Task",
    "TaskBoard",
    "TaskStatus",
    "TYPE_MESSAGE",
    "TYPE_TASK_ASSIGNMENT",
    "TYPE_TASK_CLAIMED",
    "TYPE_TASK_COMPLETED",
    "TYPE_TASK_FAILED",
    "TYPE_PLAN_APPROVAL_REQUEST",
    "TYPE_PLAN_APPROVAL_RESPONSE",
    "TYPE_SHUTDOWN_REQUEST",
    "TYPE_SHUTDOWN_APPROVED",
    "TYPE_SHUTDOWN_REJECTED",
]
