"""The SpokenLog: every line cc-voice says, and a cursor playing through it.

Nothing is dropped; the cursor just moves. Pause, resume, "again", "back
N" and pause-while-the-user-talks all fall out of the cursor:

  * append() adds a line and wakes the player.
  * pause(who) / resume(who): two independent holds, "user" (the stop
    command) and "talk" (the address word opened a turn). Playback runs
    only while neither holds. Pausing stops the line in flight; it
    replays from its start on resume.
  * replay(n): move the cursor n lines back from the line playing (or
    the last one played) and clear the user hold.

The player calls `speak(text, register)` for each entry; the callable
owns synthesis and playback (or printing, in text mode) and must honor
cancellation promptly.
"""

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Entry:
    index: int
    text: str
    register: str
    kind: str
    ts: float = field(default_factory=time.time)


class SpokenLog:
    def __init__(self, speak, *, on_start=None, on_error=None):
        self._speak = speak
        self.on_start = on_start      # (entry) as a line starts playing
        self.on_error = on_error      # (entry, exc) when a line fails
        self.entries: list[Entry] = []
        self.cursor = 0
        self.playing: int | None = None
        self._holds: set[str] = set()
        self._wake = asyncio.Event()
        self._current: asyncio.Task | None = None
        self._interrupted = False
        self._task: asyncio.Task | None = None

    # -- writing ---------------------------------------------------------

    def append(self, text: str, register: str = "speech", kind: str = "say") -> Entry:
        entry = Entry(len(self.entries), text, register, kind)
        self.entries.append(entry)
        self._wake.set()
        return entry

    # -- the cursor ------------------------------------------------------

    @property
    def paused(self) -> bool:
        return bool(self._holds)

    @property
    def holds(self) -> frozenset:
        return frozenset(self._holds)

    @property
    def busy(self) -> bool:
        return self.playing is not None

    @property
    def backlog(self) -> int:
        return len(self.entries) - self.cursor

    def pause(self, who: str = "user") -> None:
        self._holds.add(who)
        self._interrupt_current()

    def resume(self, who: str = "user") -> None:
        if who in self._holds:
            self._holds.discard(who)
            self._wake.set()

    def replay(self, n: int = 1) -> int | None:
        """Cursor to the n-th line back; None when nothing has played."""
        if self.playing is not None:
            ref = self.playing
        elif self.cursor > 0:
            ref = self.cursor - 1
        else:
            return None
        target = max(0, ref - (max(1, n) - 1))
        self._interrupt_current()
        self.cursor = target
        self._holds.discard("user")
        self._wake.set()
        return target

    def _interrupt_current(self) -> None:
        cur = self._current
        if cur is not None and not cur.done():
            self._interrupted = True
            cur.cancel()

    # -- the player ------------------------------------------------------

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())
        return self._task

    async def stop(self) -> None:
        self._interrupt_current()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        while True:
            if self.paused or self.cursor >= len(self.entries):
                self._wake.clear()
                await self._wake.wait()
                continue
            idx = self.cursor
            entry = self.entries[idx]
            self.playing = idx
            if self.on_start:
                self.on_start(entry)
            self._interrupted = False
            self._current = asyncio.ensure_future(self._speak(entry.text, entry.register))
            try:
                await self._current
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    self._current.cancel()  # the player itself is stopping
                    raise
                self._interrupted = False
                continue  # the line was paused or replayed: the cursor decides
            except Exception as exc:
                if self.on_error:
                    self.on_error(entry, exc)
                if self.cursor == idx:
                    self.cursor = idx + 1  # a bad line is skipped, not looped
            else:
                if self.cursor == idx:
                    self.cursor = idx + 1
            finally:
                self.playing = None
                self._current = None
