"""Teammate message protocol constants.

The on-disk mailbox envelope mirrors the Claude Code ``TeammateMailbox`` shape:

    {
      "from": "<sender agent name>",
      "text": "<json-encoded typed body>",
      "timestamp": "<iso8601>",
      "type": "message",
      "read": false
    }

The inner ``text`` payload is a typed message body. The ``TYPE_*`` constants
below are the canonical ``type`` discriminators used by the teammate runtime
and by teammates themselves (via ``teammate_send``).
"""

from __future__ import annotations

# ── Envelope-level types ──────────────────────────────────────────────
TYPE_MESSAGE = "message"

# ── Typed message bodies (inner ``text`` JSON ``type`` field) ─────────
TYPE_TASK_ASSIGNMENT = "task_assignment"
TYPE_TASK_CLAIMED = "task_claimed"
TYPE_TASK_COMPLETED = "task_completed"
TYPE_TASK_FAILED = "task_failed"
TYPE_PLAN_APPROVAL_REQUEST = "plan_approval_request"
TYPE_PLAN_APPROVAL_RESPONSE = "plan_approval_response"
TYPE_SHUTDOWN_REQUEST = "shutdown_request"
TYPE_SHUTDOWN_APPROVED = "shutdown_approved"
TYPE_SHUTDOWN_REJECTED = "shutdown_rejected"
TYPE_HEARTBEAT = "heartbeat"
TYPE_IDLE_NOTIFICATION = "idle_notification"
TYPE_ACK = "ack"

# ── Notify-only message types ──────────────────────────────────────────
# Messages that carry information but NEVER expect a reply. A teammate
# receiving one must not respond — there is no response slot. This mirrors
# Claude Code, where the runtime emits ``idle_notification`` via the Stop
# hook and no handler can reply to it: the courtesy-loop has no protocol
# seat, so "thanks / you're welcome" ping-pong is structurally impossible.
NOTIFY_ONLY_TYPES = frozenset({
    TYPE_TASK_CLAIMED,
    TYPE_TASK_COMPLETED,
    TYPE_TASK_FAILED,
    TYPE_IDLE_NOTIFICATION,
    TYPE_HEARTBEAT,
    TYPE_ACK,
})

# ── Field names shared by typed bodies ────────────────────────────────
FIELD_REQUEST_ID = "request_id"
FIELD_TASK_ID = "taskId"
FIELD_SUBJECT = "subject"
FIELD_DESCRIPTION = "description"
FIELD_ASSIGNEE = "assignee"
FIELD_ASSIGNED_BY = "assignedBy"
FIELD_OUTPUT = "output"
FIELD_ERROR = "error"
FIELD_REASON = "reason"
FIELD_APPROVE = "approve"
FIELD_PLAN = "plan"
FIELD_FEEDBACK = "feedback"
FIELD_IDLE_REASON = "idleReason"
FIELD_SUMMARY = "summary"
FIELD_COMPLETED_TASK_ID = "completedTaskId"

# Message envelope fields
FIELD_FROM = "from"
FIELD_TEXT = "text"
FIELD_TIMESTAMP = "timestamp"
FIELD_READ = "read"
