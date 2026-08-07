import asyncio, os, socket, sys

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
import tempfile
from pathlib import Path
from opensquilla.engine.teammate import TeammateManager
from opensquilla.teammate.llm_executor import TURN_QUEUE_FULL
from opensquilla.teammate.mailbox import Mailbox
from opensquilla.teammate.registry import TeamRegistry
from opensquilla.teammate.runtime import TeammateRuntime

class QFExecutor:
    def __init__(self): self.calls = 0
    async def run_turn(self, manager, handle, message):
        self.calls += 1
        return TURN_QUEUE_FULL

tmp = Path(tempfile.mkdtemp())
registry = TeamRegistry(tmp / "teams")
manager = TeammateManager(registry)
team = manager.registry.create_team("team-a", lead_agent_id="main", lead_session_key="lead-session")
manager.spawn_teammate(team.id, "alice", "you are alice")
handle = manager.handles.get_by_agent_id(team.id, "alice")
from opensquilla.engine.teammate import team_dir_of
inbox = Mailbox.open(team_dir_of(manager.registry, team.id), "alice")
inbox.send("team-lead", {"type": "message", "text": "m1"})
inbox.send("team-lead", {"type": "message", "text": "m2"})
inbox.send("peer", {"type": "message", "text": "m3"})

print("before poll, unread:", inbox.count_unread())

# simulate exactly what poll_once does
messages = inbox.read_unread()
print("read_unread returned", len(messages), "unread now:", inbox.count_unread())
for i, m in enumerate(messages):
    print(f"  msg{i}: from={m.from_name!r} text={m.text!r} ts={m.timestamp!r} read={m.read}")

processed_count = 0
for message in messages:
    reply = await_func = None
    processed_count += 1  # emulate: run_turn returns sentinel on first
    break

# replicate: run_turn returns TURN_QUEUE_FULL on msg0 -> mark_unread(messages[0:])
restored = inbox.mark_unread(messages[0:])
print("mark_unread restored:", restored)
print("unread after:", inbox.count_unread())
msgs = inbox.peek()
for i, m in enumerate(msgs):
    print(f"  disk{i}: from={m.from_name!r} text={m.text!r} ts={m.timestamp!r} read={m.read}")

# now check keys matching
keys = {(m.from_name, m.text, m.timestamp) for m in messages}
print("keys:", keys)
for m in msgs:
    print("tuple match:", (m.from_name, m.text, m.timestamp) in keys)
