"""端到端实测：teammate 四层防护 vs 客套循环。

场景：双成员团队 alice/bob（各 max_turns=5），lead 派任务后两人开始
客套循环（收到消息就回谢+转发给对方，模拟 LLM 用 SendMessage 互敬）。
预期：max_turns 兜底 → error；剩余 idle 成员被 idle_timeout 收敛；run() 正常退出。
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.protocol import (
    NOTIFY_ONLY_TYPES,
    TYPE_IDLE_NOTIFICATION,
    TYPE_MESSAGE,
    TYPE_SHUTDOWN_REQUEST,
    TYPE_TASK_ASSIGNMENT,
    FIELD_REQUEST_ID,
)
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import StubTeammateExecutor, TeammateRuntime


async def courteous_handler(manager, handle, body):
    """模拟 LLM 客套行为：收到普通消息 → 回谢 + 转发给对方（最坏的循环形态）。"""
    msg_type = body.get("type", "message")
    if msg_type == TYPE_SHUTDOWN_REQUEST:
        manager.approve_shutdown(
            handle.team_id, handle.name, str(body.get(FIELD_REQUEST_ID, ""))
        )
        return "shutdown approved"
    if msg_type in NOTIFY_ONLY_TYPES:
        return None  # 协议层：通知类消息没有回复位
    peer = "bob" if handle.name == "alice" else "alice"
    manager.send_message(
        handle.team_id, peer, {"type": TYPE_MESSAGE, "text": f"thanks! (from {handle.name})"}
    )
    return f"thanks! (from {handle.name})"


async def main():
    root = Path(tempfile.mkdtemp(prefix="teammate-demo-"))
    print(f"== 团队根目录: {root}")

    registry = TeamRegistry(root / "teams")
    executor = StubTeammateExecutor(courteous_handler)
    manager = TeammateManager(registry, executor=executor)
    runtime = TeammateRuntime(
        manager,
        poll_interval=0.02,
        executor=executor,
        shutdown_timeout=2.0,   # 层3b：shutdown 握手超时
        idle_timeout=0.5,       # 层3c：空闲自动收敛
    )

    team = registry.create_team(name="demo", lead_agent_id="team-lead")
    alice = manager.spawn_teammate(team.id, "alice", "you chat", max_turns=5)  # 层4
    bob = manager.spawn_teammate(team.id, "bob", "you chat", max_turns=5)
    print(f"== 成员就绪: alice(max_turns=5) / bob(max_turns=5) / idle_timeout=0.5s")

    # 1. lead 派任务
    manager.send_message(
        team.id, "alice",
        {"type": TYPE_TASK_ASSIGNMENT, "taskId": "t1", "subject": "summarize agno Team"},
    )
    print("\n== [lead] 向 alice 派发 task_assignment t1 —— 客套循环由此点燃 ==")

    t0 = time.monotonic()
    await runtime.run(team.id)  # 直到团队归零
    elapsed = time.monotonic() - t0

    # 2. 终态表
    print(f"\n== run() 退出，耗时 {elapsed:.2f}s（无空转，团队自动收敛）==")
    print(f"{'member':<8}{'status':<12}{'turns':<7}{'stop_reason':<20}{'error'}")
    for h in manager.handles.list(team.id):
        print(f"{h.name:<8}{h.status:<12}{h.turn_count:<7}{h.stop_reason:<20}{h.error or ''}")

    # 3. lead 收件箱审计：系统通道 vs 成员消息
    print("\n== lead 收件箱（层2 idle_notification 系统上报 vs 成员消息）==")
    lead = Mailbox.open(team_dir_of(registry, team.id), "team-lead")
    for m in lead.peek():
        try:
            body = json.loads(m.text)
        except json.JSONDecodeError:
            body = {"text": m.text}
        t = body.get("type", "?")
        extra = ""
        if t == TYPE_IDLE_NOTIFICATION:
            extra = f" idleReason={body.get('idleReason')} summary={body.get('summary')!r}"
        elif t == TYPE_MESSAGE:
            extra = f" text={body.get('text')!r}"
        print(f"  from={m.from_name:<6} type={t:<20}{extra}")


asyncio.run(main())
