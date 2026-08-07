import asyncio, os, gc, sys

class PipeEventLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self):
        self._ssock, self._wsock = os.pipe()
        os.set_blocking(self._ssock, False)
        os.set_blocking(self._wsock, False)
    def _close_self_pipe(self):
        if self._ssock is not None:
            os.close(self._ssock)
            self._ssock = None
        if self._wsock is not None:
            os.close(self._wsock)
            self._wsock = None
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

async def main():
    await asyncio.sleep(0.01)
    return 1

with asyncio.Runner() as r:
    print("result:", r.run(main()))

print("--- forcing gc ---")
gc.collect()
print("done")
