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
        "agent_type": {"type": "string", "description": "Optional custom agent type (from teammate_agents). If set, prompt is merged with the type's template when prompt is empty."},
    },
    required=["team_name", "name", "prompt"],
)
async def teammate_create(
    team_name: str, name: str, prompt: str, model: str | None = None, agent_type: str | None = None
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
    # Resolve agent_type template if prompt is empty or explicitly requested
    if agent_type:
        resolved = _resolve_agent_type(agent_type)
        if resolved:
            if not prompt.strip():
                prompt = resolved["prompt"]
            model = model or resolved.get("model")
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
        "agent_type": {"type": "string", "description": "Optional custom agent type (from teammate_agents)."},
    },
    required=["team_id", "name", "prompt"],
)
async def teammate_spawn(
    team_id: str, name: str, prompt: str, model: str | None = None, agent_type: str | None = None
) -> str:
    manager = _get_manager()
    team = manager.registry.get_team(team_id)
    if team is None:
        raise ToolError(f"no team '{team_id}'")
    if agent_type:
        resolved = _resolve_agent_type(agent_type)
        if resolved:
            if not prompt.strip():
                prompt = resolved["prompt"]
            model = model or resolved.get("model")
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


# ---------------------------------------------------------------------------
# teammate_steer — mid-run steering (pi-subagents parity)
# ---------------------------------------------------------------------------


@tool(
    name="teammate_steer",
    description=(
        "Steer a running teammate mid-turn: inject a new instruction into its "
        "next wake-up. The teammate receives a 'steer' message with your "
        "instruction and should adjust its plan immediately. Use this to "
        "redirect a teammate that is going off-track without killing it."
    ),
    params={
        "team_id": {"type": "string", "description": "Team id."},
        "name": {"type": "string", "description": "Teammate name to steer."},
        "instruction": {"type": "string", "description": "New instruction / correction."},
    },
    required=["team_id", "name", "instruction"],
)
async def teammate_steer(team_id: str, name: str, instruction: str) -> str:
    manager = _get_manager()
    if not instruction.strip():
        raise ToolError("instruction is required")
    from opensquilla.teammate.protocol import TYPE_STEER, FIELD_INSTRUCTION

    body: dict[str, Any] = {"type": TYPE_STEER, FIELD_INSTRUCTION: instruction}
    envelope = manager.send_message(team_id, name, body, sender="team-lead")
    return json.dumps(
        {"steered": True, "to": name, "timestamp": envelope.timestamp},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# teammate_parallel — fan-out (pi-subagents parallel)
# ---------------------------------------------------------------------------


@tool(
    name="teammate_parallel",
    description=(
        "Fan-out: spawn or reuse teammates and assign each one task in parallel. "
        "Each entry in 'tasks' needs name + prompt + description; existing "
        "members are reused, missing members are spawned. Returns the team_id "
        "and per-task assignment results. This is the primary parallel "
        "orchestration primitive (mirrors pi-subagents parallel(n))."
    ),
    params={
        "team_id": {"type": "string", "description": "Existing team id."},
        "tasks": {
            "type": "array",
            "description": "Array of {name, title, description, prompt?, model?, context?}.",
        },
    },
    required=["team_id", "tasks"],
)
async def teammate_parallel(team_id: str, tasks: list[dict[str, Any]]) -> str:
    manager = _get_manager()
    team = manager.registry.get_team(team_id)
    if team is None:
        raise ToolError(f"no team '{team_id}'")
    if not isinstance(tasks, list) or not tasks:
        raise ToolError("tasks must be a non-empty array")
    from opensquilla.teammate.protocol import TYPE_TASK_ASSIGNMENT, FIELD_TASK_ID, FIELD_SUBJECT, FIELD_DESCRIPTION, FIELD_ASSIGNEE, FIELD_ASSIGNED_BY

    board = _board(manager, team_id)
    results: list[dict[str, Any]] = []
    for entry in tasks:
        if not isinstance(entry, dict):
            raise ToolError("each task must be an object")
        name = str(entry.get("name") or "").strip()
        title = str(entry.get("title") or entry.get("subject") or "").strip()
        description = str(entry.get("description") or "").strip()
        prompt = str(entry.get("prompt") or "").strip()
        model = entry.get("model")
        context = str(entry.get("context") or "").strip()
        if not name:
            raise ToolError("each task needs 'name'")
        if not title:
            raise ToolError("each task needs 'title'")
        if not description:
            raise ToolError("each task needs 'description'")
        # Ensure teammate exists
        member = team.member(name)
        if member is None:
            if not prompt:
                prompt = f"You are {name}, a general-purpose teammate."
            manager.spawn_teammate(team_id, name, prompt, model=model if isinstance(model, str) else None)
            team = manager.registry.get_team(team_id)  # refresh
        full = description if not context else f"{description}\n\nCONTEXT:\n{context}"
        task = board.create(title=title, description=full, created_by="team-lead", assignee=name)
        body = {
            "type": TYPE_TASK_ASSIGNMENT,
            FIELD_TASK_ID: task.id,
            FIELD_SUBJECT: title,
            FIELD_DESCRIPTION: full,
            FIELD_ASSIGNEE: name,
            FIELD_ASSIGNED_BY: "team-lead",
        }
        manager.send_message(team_id, name, body, sender="team-lead")
        results.append({"name": name, "task_id": task.id, "title": title})
    return json.dumps({"team_id": team_id, "assignments": results}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# teammate_agents — custom agent types discovery
# ---------------------------------------------------------------------------


@tool(
    name="teammate_agents",
    description=(
        "List available custom agent types (.opensquilla/agents/*.md). "
        "Scans project (.opensquilla/agents) and user (~/.opensquilla/agents) scopes. "
        "Use the returned 'name' as agent_type when spawning teammates."
    ),
    params={
        "scope": {
            "type": "string",
            "description": "project | user | all (default all)",
            "enum": ["project", "user", "all"],
        },
    },
    required=[],
)
async def teammate_agents(scope: str = "all") -> str:
    import re
    from pathlib import Path

    roots: list[Path] = []
    if scope in ("project", "all"):
        # Walk up from cwd to find .opensquilla/agents
        cur = Path.cwd()
        for _ in range(6):
            cand = cur / ".opensquilla" / "agents"
            if cand.is_dir():
                roots.append(cand)
                break
            if cur.parent == cur:
                break
            cur = cur.parent
        # Also check workspace root via state_dir parent
        try:
            from opensquilla.paths import state_dir

            alt = Path(state_dir()).parent / "agents"
            if alt.is_dir() and alt not in roots:
                roots.append(alt)
        except Exception:
            pass
    if scope in ("user", "all"):
        home_agents = Path.home() / ".opensquilla" / "agents"
        if home_agents.is_dir() and home_agents not in roots:
            roots.append(home_agents)

    agents: list[dict[str, Any]] = []
    for root in roots:
        for md in sorted(root.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            # Frontmatter: ---\nname: foo\ndescription: bar\nmodel: x\n---\n
            fm: dict[str, str] = {}
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    for line in text[3:end].splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            fm[k.strip().lower()] = v.strip().strip('"').strip("'")
            name = fm.get("name") or md.stem
            desc = fm.get("description") or text.strip().splitlines()[0][:120] if text.strip() else ""
            # Strip markdown heading
            desc = re.sub(r"^#+\s*", "", desc).strip()
            agents.append(
                {
                    "name": name,
                    "description": desc,
                    "model": fm.get("model") or None,
                    "path": str(md),
                }
            )
    return json.dumps(agents, ensure_ascii=False)


# ── pipeline (serial orchestration) ──────────────────────────────────


@tool(
    name="teammate_pipeline",
    description=(
        "Serial pipeline: assign tasks to teammates in order, each task's "
        "description can reference the previous task's output via {{prev_output}} "
        "placeholder. Tasks run in the order given; the pipeline returns "
        "all assignments. For parallel fan-out use teammate_parallel."
    ),
    params={
        "team_id": {"type": "string", "description": "Existing team id."},
        "tasks": {
            "type": "array",
            "description": "Array of {name, title, description, prompt?, model?, context?}.",
        },
    },
    required=["team_id", "tasks"],
)
async def teammate_pipeline(team_id: str, tasks: list[dict[str, Any]]) -> str:
    manager = _get_manager()
    team = manager.registry.get_team(team_id)
    if team is None:
        raise ToolError(f"no team '{team_id}'")
    if not isinstance(tasks, list) or not tasks:
        raise ToolError("tasks must be a non-empty array")
    from opensquilla.teammate.protocol import TYPE_TASK_ASSIGNMENT, FIELD_TASK_ID, FIELD_SUBJECT, FIELD_DESCRIPTION, FIELD_ASSIGNEE, FIELD_ASSIGNED_BY

    board = _board(manager, team_id)
    results: list[dict[str, Any]] = []
    for entry in tasks:
        if not isinstance(entry, dict):
            raise ToolError("each task must be an object")
        name = str(entry.get("name") or "").strip()
        title = str(entry.get("title") or "").strip()
        description = str(entry.get("description") or "").strip()
        prompt = str(entry.get("prompt") or "").strip()
        model = entry.get("model")
        if not name or not title or not description:
            raise ToolError("each task needs 'name', 'title', 'description'")
        member = team.member(name)
        if member is None:
            if not prompt:
                prompt = f"You are {name}, a general-purpose teammate."
            manager.spawn_teammate(team_id, name, prompt, model=model if isinstance(model, str) else None)
            team = manager.registry.get_team(team_id)
        task = board.create(title=title, description=description, created_by="team-lead", assignee=name)
        body = {
            "type": TYPE_TASK_ASSIGNMENT,
            FIELD_TASK_ID: task.id,
            FIELD_SUBJECT: title,
            FIELD_DESCRIPTION: description,
            FIELD_ASSIGNEE: name,
            FIELD_ASSIGNED_BY: "team-lead",
        }
        manager.send_message(team_id, name, body, sender="team-lead")
        results.append({"name": name, "task_id": task.id, "title": title, "seq": len(results)})
    return json.dumps({"team_id": team_id, "pipeline": results}, ensure_ascii=False)


# ── helpers ────────────────────────────────────────────────────────────

def _resolve_agent_type(agent_type: str) -> dict[str, Any] | None:
    """Resolve a custom agent type from .opensquilla/agents/*.md."""
    import re
    from pathlib import Path

    name = str(agent_type or "").strip()
    if not name:
        return None
    candidates: list[Path] = []
    # project scope
    cur = Path.cwd()
    for _ in range(6):
        cand = cur / ".opensquilla" / "agents" / f"{name}.md"
        if cand.is_file():
            candidates.append(cand)
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    # user scope
    home = Path.home() / ".opensquilla" / "agents" / f"{name}.md"
    if home.is_file() and home not in candidates:
        candidates.append(home)
    if not candidates:
        return None
    try:
        text = candidates[0].read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip().lower()] = v.strip().strip('"').strip("'")
            body = text[end + 4 :].strip()
    return {"prompt": body.strip() or text.strip(), "model": fm.get("model")}


def _board(manager: Any, team_id: str):
    from opensquilla.teammate.taskboard import TaskBoard
    from opensquilla.engine.teammate import team_dir_of

    return TaskBoard(team_dir_of(manager.registry, team_id) / "tasks.json")
