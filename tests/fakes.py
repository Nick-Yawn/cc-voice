"""Shared offline fakes: a scripted claude child and stream records."""

import asyncio
import json

SID = "db54779a-0000-4000-8000-000000000000"


def j(obj) -> str:
    return json.dumps(obj) + "\n"


def init(sid=SID):
    return j({"type": "system", "subtype": "init", "session_id": sid})


def echo(text):
    return j({"type": "user", "message": {"role": "user",
                                          "content": [{"type": "text", "text": text}]}})


def assistant_tool(name="Read", inp=None):
    return j({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp or {"file_path": "a.py"}}]}})


def assistant_text(text):
    return j({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def result(text="done", used=None, window=None, is_error=False, origin=None,
           num_turns=None, duration_ms=1500):
    rec = {"type": "result", "is_error": is_error, "result": text,
           "duration_ms": duration_ms}
    if used is not None:
        rec["usage"] = {"input_tokens": used, "output_tokens": 0}
        rec["modelUsage"] = {"m": {"contextWindow": window}}
    if origin:
        rec["origin"] = {"kind": origin}
    if num_turns is not None:
        rec["num_turns"] = num_turns
    return j(rec)


class FakeStdin:
    def __init__(self, fail=False):
        self.writes = []
        self.closed = False
        self.fail = fail

    def write(self, data: bytes):
        if self.fail:
            raise BrokenPipeError("stdin gone")
        self.writes.append(data.decode("utf-8"))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    async def readline(self) -> bytes:
        item = await self.q.get()
        if isinstance(item, Exception):
            raise item
        return item

    def feed(self, *lines: str):
        for line in lines:
            self.q.put_nowait(line.encode("utf-8"))

    def fail(self, exc: Exception):
        self.q.put_nowait(exc)

    def eof(self):
        self.q.put_nowait(b"")


class FakeProc:
    pid = None

    def __init__(self, fail_stdin=False):
        self.stdin = FakeStdin(fail=fail_stdin)
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.returncode = None
        self._exit = asyncio.get_event_loop().create_future()
        self.killed = False

    async def wait(self):
        return await asyncio.shield(self._exit)

    def kill(self):
        self.killed = True
        self.die(-9)

    def die(self, rc: int):
        if self.returncode is None:
            self.returncode = rc
            self._exit.set_result(rc)

    def exit(self, rc: int):
        self.die(rc)
        self.stdout.eof()
        self.stderr.eof()


class ScriptedClaude:
    """A spawn seam whose children answer each written message with a
    scripted stream: `answers` maps a message text to the lines to feed
    after its echo. Children exit cleanly on stdin close."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.spawned: list[tuple] = []
        self.procs: list[FakeProc] = []

    async def spawn(self, cmd, cwd, env):
        proc = FakeProc()
        self.spawned.append((cmd, cwd, env))
        self.procs.append(proc)
        # Answer writes as they land: a watcher task keyed to this proc.
        asyncio.ensure_future(self._answer(proc))
        return proc

    async def _answer(self, proc: FakeProc):
        seen = 0
        while proc.returncode is None:
            if proc.stdin.closed:
                proc.exit(0)
                return
            if len(proc.stdin.writes) > seen:
                line = proc.stdin.writes[seen]
                seen += 1
                text = json.loads(line)["message"]["content"][0]["text"]
                lines = self.answers.get(text)
                if lines is not None:
                    proc.stdout.feed(init(), echo(text), *lines)
            await asyncio.sleep(0.001)


async def until(pred, tries=3000):
    # 6s nominal budget: under load, several scenario steps in test_voice.py
    # measured right up against the old 500-try (1s) budget and failed
    # flakily, not because the condition was slow to become true in wall
    # time but because the event loop had other tasks (a talking fake mic,
    # a polling scripted claude) to get through first.
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition never held")
