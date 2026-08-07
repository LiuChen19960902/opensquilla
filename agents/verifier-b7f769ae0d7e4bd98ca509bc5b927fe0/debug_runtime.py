import asyncio, os, sys, tempfile
from pathlib import Path

class PipeEventLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self):
        self._ssock, self._wsock = os.pipe()
        os.set_blocking(self._ssock, False); os.set_blocking(self._wsock, False)
    def _close_self_pipe(self):
        os.close(self._ssock); os.close(self._wsock)
        self._ssock = None; self._wsock = None
    def _write_to_self(self):
        if self._wsock is not None:
            try: os.write(self._wsock, b'\0')
            except (BlockingIOError, InterruptedError, ConnectionError, OSError): pass
    def _read_from_self(self):
        try:
            data = os.read(self._ssock, 4096)
            if not data: return
            self._process_self_data(data)
        except (BlockingIOError, InterruptedError): pass

class PipePolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self): return PipeEventLoop()

asyncio.set_event_loop_policy(PipePolicy())
sys.path.insert(0, "/workpace/opensquilla/src")

from opensquilla.engine.teammate import TeammateManager, team_dir_of
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import TeammateRuntime

class QFExecutor:
    def __init__(self): self.calls = 0
    async def run_turn(self, manager, handle, message):
        self.calls += 1
        print(f"    run_turn called with msg text={message.text!r}")
        return TURN_QUEUE_FULL

async def main():
    tmp = Path(tempfile.mkdtemp())
    registry = TeamRegistry(tmp / "teams")
    manager = TeammateManager(registry)
    team = manager.registry.create_team("team-a", lead_agent_id="main", lead_session_key="lead-session")
    manager.spawn_teammate(team.id, "alice", "you are alice")
    handle = manager.handles.get_by_agent_id(team.id, "alice")
    inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "alice")
    inbox.send("team-lead", {"type": "message", "text": "m1"})
    inbox.send("team-lead", {"type": "message", "text": "m2"})
    inbox.send("peer", {"type": "message", "text": "m3"})
    print("team_dir:", team_dir_of(manager.registry, team.id))
    print("handle.team_dir:", handle.team_dir)
    print("before poll unread:", inbox.count_unread())

    executor = QFExecutor()
    runtime = TeammateRuntime(manager, executor=executor, poll_interval=0.01)
    replies = await runtime.poll_once(team.id)
    print("replies:", replies)
    print("executor.calls:", executor.calls)
    print("after poll unread:", inbox.count_unread())
    for i, m in enumerate(inbox.peek()):
        print(f"  disk{i}: from={m.from_name!r} text={m.text!r} read={m.read}")
    print("handle.status:", handle.status, "stop_reason:", handle.stop_reason)

asyncio.run(main())
