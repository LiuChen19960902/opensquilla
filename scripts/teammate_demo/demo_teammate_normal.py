"""对照：正常团队 —— 完成任务后安静收敛（默认 handler，无客套）。"""
import asyncio, json, tempfile, time
from pathlib import Path
from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import TYPE_IDLE_NOTIFICATION, TYPE_MESSAGE, TYPE_TASK_ASSIGNMENT
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import StubTeammateExecutor, TeammateRuntime

async def main():
    registry = TeamRegistry(Path(tempfile.mkdtemp(prefix="teammate-normal-")) / "teams")
    manager = TeammateManager(registry, executor=StubTeammateExecutor())
    runtime = TeammateRuntime(manager, poll_interval=0.02, idle_timeout=0.5)
    team = registry.create_team(name="normal", lead_agent_id="team-lead")
    for name in ("worker-a", "worker-b"):
        manager.spawn_teammate(team.id, name, f"you are {name}")

    # 正常派活：每个成员一个任务
    manager.send_message(team.id, "worker-a", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "1", "subject": "write docs"})
    manager.send_message(team.id, "worker-b", {"type": TYPE_TASK_ASSIGNMENT, "taskId": "2", "subject": "review code"})

    t0 = time.monotonic()
    await runtime.run(team.id)
    print(f"== run() 退出，耗时 {time.monotonic()-t0:.2f}s ==")
    for h in manager.handles.list(team.id):
        print(f"  {h.name:<10} status={h.status:<10} stop={h.stop_reason} turns={h.turn_count}")
    lead = Mailbox.open(team_dir_of(registry, team.id), "team-lead")
    print(f"== lead 收件箱 {lead.count_unread()} 条未读 ==")
    for m in lead.peek():
        body = json.loads(m.text)
        t = body.get("type")
        if t == TYPE_IDLE_NOTIFICATION:
            print(f"  [sys] from={m.from_name:<10} idleReason={body.get('idleReason')} summary={body.get('summary')!r}")
        elif t == TYPE_MESSAGE:
            print(f"  [msg] from={m.from_name:<10} text={body.get('text')!r}")

asyncio.run(main())
