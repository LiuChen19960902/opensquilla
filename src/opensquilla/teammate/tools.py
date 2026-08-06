"""Teammate tools — registered into the global ToolRegistry via @tool.

Tool surface (P0):
    teammate_create        — create a team and spawn a resident teammate
    teammate_send          — send a typed message to a teammate's mailbox
    teammate_task_create   — add a task to the shared task board
    teammate_task_list     — list tasks (optionally by status/assignee)
    teammate_task_update   — claim / complete / fail a task
    teammate_status        — per-member status summary for a team

The handler surface mirrors ``tools/builtin/sessions.py``: module-level
setters inject the manager from gateway boot; until injected the tools raise
``ToolError`` so the catalog is safe in any context.
"""

from __future__ import annotations

import json
from typing import Any

from opensquilla.teammate.protocol import (
    TYPE_IDLE_NOTIFICATION,
    TYPE_TASK_ASSIGNMENT,
    FIELD_TASK_ID,
    FIELD_SUBJECT,
    FIELD_DESCRIPTION,
    FIELD_ASSIGNEE,
    FIELD_ASSIGNED_BY,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.types import ToolError, current_tool_context

# ── injected dependencies (set from gateway boot) ──────────────────────
_manager: Any = None


def set_teammate_manager(manager: Any) -> None:
    """Inject the TeammateManager instance (called from gateway boot)."""
    global _manager
    _manager = manager


def teammate_manager_available() -> bool:
    return _manager is not None


def _get_manager() -> Any:
    if _manager is None:
        raise ToolError(
            "Teammate subsystem is not wired yet (P1 gateway integration pending)"
        )
    return _manager


def _team_id_of(params: dict[str, Any]) -> str:
    team_id = (params.get("team_id") or "").strip()
    if not team_id:
        raise ToolError("team_id is required")
    return team_id


# ---------------------------------------------------------------------------
# teammate_create
# ---------------------------------------------------------------------------


@tool(
    name="teammate_create",
    description=(
        "Create a team and spawn a resident teammate. Returns the team_id and "
        "the teammate's agent_id. The teammate stays alive (idle) until it "
        "receives messages via teammate_send or a shutdown request."
    ),
    params={
        "team_name": {"type": "string", "description": "Human-readable team name."},
        "name": {"type": "string", "description": "Teammate name (unique within the team)."},
        "prompt": {"type": "string", "description": "Teammate role/system prompt."},
        "model": {"type": "string", "description": "Optional model override."},
    },
    required=["team_name", "name", "prompt"],
)
async def teammate_create(
    team_name: str, name: str, prompt: str, model: str | None = None
) -> str:
    manager = _get_manager()
    # Record the caller's session as the team lead's visible session so the
    # resident runtime can stream teammate replies onto the lead's screen
    # (Claude Code-style shared transcript) via the visible sink.
    ctx = current_tool_context.get()
    lead_session_key = (ctx.session_key if ctx is not None else None) or ""
    team = manager.registry.create_team(
        name=team_name,
        lead_agent_id="team-lead",
        lead_session_key=lead_session_key,
    )
    handle = manager.spawn_teammate(team.id, name, prompt, model=model)
    return json.dumps(
        {"team_id": team.id, "agent_id": handle.agent_id, "status": handle.status},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_spawn — add a member to an existing team
# ---------------------------------------------------------------------------


@tool(
    name="teammate_spawn",
    description=(
        "Add a resident teammate to an EXISTING team (team_id required). "
        "Returns the teammate's agent_id. The teammate stays alive (idle) "
        "until it receives messages via teammate_send or a shutdown request."
    ),
    params={
        "team_id": {"type": "string", "description": "Existing team id."},
        "name": {"type": "string", "description": "Teammate name (unique within the team)."},
        "prompt": {"type": "string", "description": "Teammate role/system prompt."},
        "model": {"type": "string", "description": "Optional model override."},
    },
    required=["team_id", "name", "prompt"],
)
async def teammate_spawn(
    team_id: str, name: str, prompt: str, model: str | None = None
) -> str:
    manager = _get_manager()
    team = manager.registry.get_team(team_id)
    if team is None:
        raise ToolError(f"no team '{team_id}'")
    handle = manager.spawn_teammate(team.id, name, prompt, model=model)
    return json.dumps(
        {"team_id": team.id, "agent_id": handle.agent_id, "status": handle.status},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_send
# ---------------------------------------------------------------------------


@tool(
    name="teammate_send",
    description=(
        "Send a typed message to a teammate's mailbox. The message body must be "
        "a JSON object; use type 'task_assignment' with subject/description to "
        "assign work. Replies are routed back to the sender's own mailbox "
        "(peer-to-peer): when the team-lead sends, the reply lands in the lead "
        "inbox; when a teammate sends to another teammate, the reply lands in "
        "the sender teammate's inbox (lateral communication)."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id from teammate_create."},
        "to": {"type": "string", "description": "Recipient: a teammate name (or 'team-lead')."},
        "message": {"type": "object", "description": "Typed message body (JSON object)."},
        "sender": {
            "type": "string",
            "description": "Sender identity. Defaults to 'team-lead'. A teammate "
            "running inside the team sets this to its own name to talk to peers.",
        },
    },
    required=["team_id", "to", "message"],
)
async def teammate_send(team_id: str, to: str, message: dict[str, Any], sender: str = "team-lead") -> str:
    manager = _get_manager()
    if not isinstance(message, dict):
        raise ToolError("message must be a JSON object")
    body: dict[str, Any] = dict(message)
    body.setdefault("type", "message")
    envelope = manager.send_message(team_id, to, body, sender=sender)
    return json.dumps(
        {"delivered": True, "from": sender, "to": to, "timestamp": envelope.timestamp},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_task_create
# ---------------------------------------------------------------------------


@tool(
    name="teammate_task_create",
    description=(
        "Create a task on the team's shared task board. New tasks are 'pending' "
        "with no owner; idle teammates claim them via teammate_task_update."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "title": {"type": "string", "description": "Brief actionable title."},
        "description": {"type": "string", "description": "What needs to be done."},
        "assignee": {"type": "string", "description": "Optional assignee name."},
    },
    required=["team_id", "title"],
)
async def teammate_task_create(
    team_id: str, title: str, description: str = "", assignee: str | None = None
) -> str:
    manager = _get_manager()
    board = _board(manager, team_id)
    task = board.create(
        title=title, description=description, created_by="team-lead", assignee=assignee
    )
    return json.dumps(task.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_assign
# ---------------------------------------------------------------------------


@tool(
    name="teammate_assign",
    description=(
        "Assign a task to a specific teammate AND send the task_assignment "
        "message in one step (task board + mailbox). Use this when you know "
        "which teammate should do the work — it creates a task on the shared "
        "board and delivers the full description (plus optional context/notes) "
        "to the teammate's mailbox so their next wake-up works on it. This is "
        "the lead's primary 'decompose & delegate' tool: call it once per "
        "sub-task after breaking down a requirement."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "name": {"type": "string", "description": "Teammate name to assign to."},
        "title": {"type": "string", "description": "Brief task title (board)."},
        "description": {"type": "string", "description": "Detailed task / what to do."},
        "context": {
            "type": "string",
            "description": "Optional extra context (file paths, code snippets, "
            "requirements, related conversation) delivered to the teammate.",
        },
    },
    required=["team_id", "name", "title", "description"],
)
async def teammate_assign(
    team_id: str, name: str, title: str, description: str, context: str = ""
) -> str:
    manager = _get_manager()
    board = _board(manager, team_id)
    full = description if not context else f"{description}\n\nCONTEXT:\n{context}"
    task = board.create(
        title=title, description=full, created_by="team-lead", assignee=name
    )
    body = {
        "type": TYPE_TASK_ASSIGNMENT,
        FIELD_TASK_ID: task.id,
        FIELD_SUBJECT: title,
        FIELD_DESCRIPTION: full,
        FIELD_ASSIGNEE: name,
        FIELD_ASSIGNED_BY: "team-lead",
    }
    manager.send_message(team_id, name, body, sender="team-lead")
    return json.dumps(
        {
            "assigned": True,
            "task": task.to_dict(),
            "delivered_to": name,
            "status": "pending",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_task_list
# ---------------------------------------------------------------------------


@tool(
    name="teammate_task_list",
    description=(
        "List tasks on the team's shared task board, optionally filtered by "
        "status (pending/in_progress/completed/failed/blocked) or assignee."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "status": {"type": "string", "description": "Optional status filter."},
        "assignee": {"type": "string", "description": "Optional assignee filter."},
    },
    required=["team_id"],
)
async def teammate_task_list(
    team_id: str, status: str | None = None, assignee: str | None = None
) -> str:
    manager = _get_manager()
    board = _board(manager, team_id)
    tasks = board.list(status=status, assignee=assignee)
    return json.dumps([t.to_dict() for t in tasks], ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_task_update
# ---------------------------------------------------------------------------


@tool(
    name="teammate_task_update",
    description=(
        "Update a task on the team's shared task board. Use action='claim' to "
        "take an unassigned pending task, 'complete' to report completion, "
        "or 'fail' to report failure."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "task_id": {"type": "string", "description": "Task id from teammate_task_create."},
        "action": {
            "type": "string",
            "description": "claim | complete | fail",
            "enum": ["claim", "complete", "fail"],
        },
        "assignee": {"type": "string", "description": "Required for claim."},
        "output": {"type": "string", "description": "Result/error text for complete/fail."},
    },
    required=["team_id", "task_id", "action"],
)
async def teammate_task_update(
    team_id: str,
    task_id: str,
    action: str,
    assignee: str | None = None,
    output: str = "",
) -> str:
    manager = _get_manager()
    board = _board(manager, team_id)
    try:
        if action == "claim":
            if not assignee:
                raise ToolError("assignee is required for claim")
            task = board.claim(task_id, assignee)
        elif action == "complete":
            task = board.complete(task_id, output)
        elif action == "fail":
            task = board.fail(task_id, output)
        else:
            raise ToolError(f"unknown action: {action}")
    except (ValueError, KeyError) as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(task.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_list
# ---------------------------------------------------------------------------


@tool(
    name="teammate_list",
    description=(
        "List all teams with their members and statuses. Use this to recover "
        "a team_id (e.g. after a gateway restart) before calling the other "
        "teammate_* tools."
    ),
    params={},
    required=[],
)
async def teammate_list() -> str:
    manager = _get_manager()
    teams = []
    for team in manager.registry.list_teams():
        teams.append(
            {
                "team_id": team.id,
                "name": team.name,
                "lead_agent_id": team.lead_agent_id,
                "members": [
                    {"name": m.name, "status": m.status, "maxTurns": m.max_turns}
                    for m in team.members
                ],
            }
        )
    return json.dumps(teams, ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_inbox
# ---------------------------------------------------------------------------


@tool(
    name="teammate_inbox",
    description=(
        "Read a team member's inbox: replies and system notifications "
        "(idle_notification with idleReason/summary) sent to them. "
        "Defaults to the team lead's inbox; pass 'name' to read a teammate's "
        "inbox (e.g. to collect a member's finished work for integration). "
        "Each entry has from / type / text or summary / timestamp. "
        "Reads do not mark messages as read."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "name": {
            "type": "string",
            "description": "Inbox owner: a teammate name or 'team-lead' (default).",
        },
    },
    required=["team_id"],
)
async def teammate_inbox(team_id: str, name: str = "team-lead") -> str:
    manager = _get_manager()
    from opensquilla.engine.teammate import team_dir_of
    from opensquilla.teammate.mailbox import Mailbox

    inbox = Mailbox.open(team_dir_of(manager.registry, team_id), name)
    entries = []
    for msg in inbox.peek():
        entry: dict[str, Any] = {"from": msg.from_name, "type": msg.type, "timestamp": msg.timestamp}
        try:
            body = json.loads(msg.text) if isinstance(msg.text, str) else msg.text
        except (ValueError, TypeError):
            body = {"text": msg.text}
        if msg.type == TYPE_IDLE_NOTIFICATION:
            entry["idleReason"] = body.get("idleReason")
            entry["summary"] = body.get("summary")
        else:
            entry["text"] = body.get("text") if isinstance(body, dict) else body
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_shutdown
# ---------------------------------------------------------------------------


@tool(
    name="teammate_shutdown",
    description=(
        "Shut down a teammate. By default this is a cooperative handshake: "
        "the teammate receives a shutdown_request and approves it (done). "
        "Set force=true to kill it immediately without waiting for approval "
        "(aborted) — use when the teammate is stuck or you want it gone now."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "name": {"type": "string", "description": "Teammate name."},
        "reason": {"type": "string", "description": "Optional shutdown reason."},
        "force": {"type": "boolean", "description": "Force-kill without handshake (default false)."},
    },
    required=["team_id", "name"],
)
async def teammate_shutdown(
    team_id: str, name: str, reason: str = "", force: bool = False
) -> str:
    manager = _get_manager()
    if force:
        manager.force_shutdown(team_id, name, reason=reason or "force_shutdown")
        return json.dumps({"killed": True, "team_id": team_id, "name": name}, ensure_ascii=False)
    request_id = manager.request_shutdown(team_id, name, reason=reason)
    return json.dumps(
        {"requested": True, "request_id": request_id, "team_id": team_id, "name": name},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_status
# ---------------------------------------------------------------------------


@tool(
    name="teammate_status",
    description="Show per-member status for a team (idle/running/done/...).",
    params={"team_id": {"type": "string", "description": "Team id."}},
    required=["team_id"],
)
async def teammate_status(team_id: str) -> str:
    manager = _get_manager()
    return json.dumps(manager.status(team_id), ensure_ascii=False)


# ── helpers ────────────────────────────────────────────────────────────


def _board(manager: Any, team_id: str):
    from opensquilla.teammate.taskboard import TaskBoard
    from opensquilla.engine.teammate import team_dir_of

    return TaskBoard(team_dir_of(manager.registry, team_id) / "tasks.json")
