"""Offline providers: a scripted STT and a silent TTS, for tests and for
running the voice loop without keys."""

import asyncio

from cc_voice.providers import STTCaps, TTSCaps


class FakeSTTSession:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.bytes_sent = 0
        self.closed = False
        self._ended = False

    async def send(self, pcm16: bytes) -> None:
        self.bytes_sent += len(pcm16)

    async def events(self):
        try:
            while True:
                ev = await self.queue.get()
                if ev is None:
                    return
                yield ev
        finally:
            self._ended = True

    async def close(self) -> None:
        self.closed = True
        if not self._ended:
            self.queue.put_nowait(None)  # wake a waiting events() only


class FakeSTT:
    """open() hands back a session fed from `self.queue`: tests put
    STT events on it; None ends the session (a link drop)."""

    caps = STTCaps(partials=True, word_timings=False, native_turns=False,
                   keyterms=True, streaming=True)

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sessions: list[FakeSTTSession] = []
        self.opened_with: list[dict] = []

    async def open(self, rate: int = 16000, keyterms=(), language: str = "en"):
        self.opened_with.append({"rate": rate, "keyterms": list(keyterms),
                                 "language": language})
        session = FakeSTTSession(self.queue)
        self.sessions.append(session)
        return session


class FakeTTS:
    """Yields a few silent PCM chunks per sentence, slowly enough that a
    cancel mid-line is observable; records every text it was asked."""

    caps = TTSCaps()
    sample_rate = 24000

    def __init__(self, chunk_delay_s: float = 0.0, chunks: int = 2):
        self.spoken: list[tuple[str, str | None]] = []
        self.cancelled: list[str] = []
        self.chunk_delay_s = chunk_delay_s
        self.chunks = chunks
        self.fail_on: set[str] = set()

    async def speak(self, text: str, *, voice: str | None = None):
        self.spoken.append((text, voice))
        if text in self.fail_on:
            raise RuntimeError("fake vendor error")
        try:
            for _ in range(self.chunks):
                if self.chunk_delay_s:
                    await asyncio.sleep(self.chunk_delay_s)
                yield b"\x00\x00" * 240
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
