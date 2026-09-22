"""The voice front end.

  mic -> STT session -> TurnMachine -> Host (Seat)
  Seat events -> SpokenLog -> TTS -> Playback (speaker)

Plus the earcons, the closer's settle window, pause-while-talking, the
mic-starvation watchdog, and STT reconnects. Every piece of audio
hardware and every vendor is injectable, so the whole loop runs offline
in tests with fake providers.
"""

import asyncio
import contextlib
import sys
import time

from earshot.app import Host
from earshot.audio import MIC_RATE, Mic, Playback, watchdog_tick
from earshot.earcons import get_set
from earshot.providers import Error, Final, Partial, SpeechStarted, TurnEnd
from earshot.scrub import Scrubber
from earshot.spoken_log import SpokenLog
from earshot.text import Respeller
from earshot.turns import TurnMachine

_REBUILD = object()  # a frame-queue sentinel: end this STT pass cleanly
MIC_CONSTRUCT_MAX_FAILURES = 5
WATCHDOG_INTERVAL_S = 2.0
ABANDON_INTERVAL_S = 5.0


def make_speaker(tts, playback: Playback, scrubber: Scrubber, respell: Respeller,
                 volumes: dict, voice: str | None):
    """The SpokenLog's speak(): scrub and respell, synthesize, play at the
    register's volume, and return once the audio has played out. A
    cancel (pause, replay, quit) aborts queued audio at once."""

    async def speak(text: str, register: str) -> None:
        audio_text = scrubber(respell(text))
        gain = float(volumes.get(register, 1.0))
        try:
            async with contextlib.aclosing(tts.speak(audio_text, voice=voice)) as chunks:
                carry = b""
                async for pcm in chunks:
                    buf = carry + pcm
                    if len(buf) % 2:  # frame-align: a half sample would raise mid-word
                        buf, carry = buf[:-1], buf[-1:]
                    else:
                        carry = b""
                    playback.play(buf, gain)
            await playback.wait_played()
        except asyncio.CancelledError:
            playback.abort()
            raise

    return speak


class VoiceFront:
    def __init__(self, host: Host, cfg: dict, *, stt, tts, playback: Playback, mic: Mic,
                 clock=time.monotonic):
        self.host = host
        self.cfg = cfg
        self.stt = stt
        self.tts = tts
        self.playback = playback
        self.mic = mic
        self._clock = clock
        self._out = host._out
        words = cfg["words"]
        turns = cfg["turns"]
        self.address = words["address"]
        self.closer = words["closer"]
        self.language = cfg["stt"].get("language", "en")
        self.settle_s = float(turns["closer_settle_s"])
        self.machine = TurnMachine(
            self.address, self.closer,
            filler_before_address=bool(words.get("filler_before_address")),
            readdress_gap_s=float(turns["readdress_gap_s"]),
            idle_turn_s=float(turns["idle_turn_s"]), clock=clock)
        self.earcons = get_set(playback.rate)
        self.earcon_gain = float(cfg["volumes"].get("earcons", 1.0))
        self.link_up = False
        self._settle: asyncio.Task | None = None
        self.frames_q: asyncio.Queue | None = None
        self.pass_start = clock()
        self._tasks: list[asyncio.Task] = []
        host.status_extra = self.status_extra
        host.on_still_here = lambda: self.cue("still_here")

    # -- cues ------------------------------------------------------------------

    def cue(self, name: str) -> None:
        self.playback.play(self.earcons[name], self.earcon_gain)

    def _link(self, up: bool) -> None:
        if up == self.link_up:
            return
        self.link_up = up
        self.cue("connected" if up else "disconnected")
        self.host.log.write("link", up=up)

    def status_extra(self) -> dict:
        t = self.mic.last_frame_t
        return {"link_up": self.link_up,
                "mic_age_s": (self._clock() - t) if t is not None else None}

    # -- the turn machine's actions --------------------------------------------

    def handle_actions(self, acts: list[tuple]) -> None:
        for act in acts:
            kind = act[0]
            if kind == "drop":
                self._out(f"  ∅ ignored: {act[1]}")
                self.host.log.write("dropped", text=act[1])
            elif kind == "open":
                self.host.spoken.pause("talk")
                self.cue("capture")
                self._out(f"you ▸ {act[1] or ''}")
                self.host.log.write("open", text=act[1])
            elif kind == "append":
                self._out(f"      {act[1]}")
            elif kind == "closing":
                pass  # the settle window is armed below
            elif kind == "dispatch":
                self.host.spoken.resume("talk")
                if act[1]:
                    self.cue("dispatch")
                    self.host.enqueue_turn(act[1])
                else:
                    self._out("  (empty turn, nothing sent)")
            elif kind == "command":
                self.host.spoken.resume("talk")
                self.command(act[1], act[2])
            elif kind == "abandoned":
                self.host.spoken.resume("talk")
                self.cue("abandoned")
                self._out(f"  [discarded: {act[1]}]")
                self.host.log.write("abandoned", text=act[1])
        self._sync_settle()

    def _sync_settle(self) -> None:
        """Arm the closer's silence window while the machine is CLOSING
        (a silent final re-arms it); drop it once the machine is not."""
        closing = self.machine.state == TurnMachine.CLOSING
        if closing and self._settle is None:
            self._arm_settle(self.settle_s)
        elif not closing:
            self._cancel_settle()

    def _arm_settle(self, seconds: float) -> None:
        self._cancel_settle()
        self._settle = asyncio.ensure_future(self._settle_after(seconds))

    def _cancel_settle(self) -> None:
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None

    async def _settle_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._settle = None
        self.handle_actions(self.machine.settle())

    @property
    def speech_hold_s(self) -> float:
        """How long a speech onset (no words yet) holds a pending closer:
        long enough for the words to arrive, short enough that a cough
        after the closer cannot hang the turn."""
        return max(self.settle_s, 1.5)

    def on_stt_event(self, ev) -> None:
        closing = self.machine.state == TurnMachine.CLOSING
        if isinstance(ev, Partial):
            if ev.text and closing:
                self._cancel_settle()  # more words: the next final decides
        elif isinstance(ev, SpeechStarted):
            if closing:
                self._arm_settle(self.speech_hold_s)
        elif isinstance(ev, (Final, TurnEnd)):
            text = (ev.text or "").strip()
            if text:
                self.host.log.write("heard", text=text)
                self.handle_actions(self.machine.feed(text))
            elif closing:
                # silence ended a segment with no words: whatever held the
                # closer was noise, so the normal window runs from here
                self._arm_settle(self.settle_s)

    # -- local commands ------------------------------------------------------------

    def command(self, name: str, arg) -> None:
        host = self.host
        host.log.write("command", name=name, arg=arg)
        if name == "stop":
            host.spoken.pause("user")
            self._out("  · stopped")
        elif name == "resume":
            host.spoken.resume("user")
            self._out("  · resumed")
        elif name == "again":
            if host.spoken.replay(arg or 1) is None:
                host.say_local("Nothing to replay yet.")
        elif name == "status":
            host.command_status()
        elif name == "cancel":
            self._out("  · cancelled")
        elif name == "compact":
            host.command_compact()
        elif name == "quit":
            self._out("  · quit")
            asyncio.ensure_future(host.quit())

    # -- the ears --------------------------------------------------------------------

    async def _pump(self, frames_q: asyncio.Queue, session) -> None:
        while True:
            chunk = await frames_q.get()
            if chunk is _REBUILD:
                await session.close()
                return
            await session.send(chunk)

    async def ears(self) -> None:
        loop = asyncio.get_running_loop()
        quitting = self.host.quitting
        backoff = 1.0
        mic_failures = 0
        while not quitting.is_set():
            frames_q: asyncio.Queue = asyncio.Queue()
            self.frames_q = frames_q
            self.pass_start = self._clock()
            try:
                self.mic.start(loop, frames_q.put_nowait)
            except Exception as exc:
                mic_failures += 1
                self._out(f"[ears: mic failed ({exc!r}); retrying]")
                self.mic.stop()
                if mic_failures >= MIC_CONSTRUCT_MAX_FAILURES:
                    raise
                await asyncio.sleep(1.0)
                continue
            mic_failures = 0
            try:
                session = await self.stt.open(rate=self.mic.rate,
                                              keyterms=[self.address, self.closer],
                                              language=self.language)
            except Exception as exc:
                self._out(f"[ears: speech-to-text link failed ({exc!r});"
                          f" retrying in {backoff:.0f}s]")
                self.mic.stop()
                self._link(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            self._link(True)
            pump = asyncio.ensure_future(self._pump(frames_q, session))
            try:
                async for ev in session.events():
                    if quitting.is_set():
                        break
                    if isinstance(ev, Error):
                        self._out(f"[ears: {ev.message}; reconnecting]")
                        break
                    self.on_stt_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._out(f"[ears: transcription dropped ({exc!r}); reconnecting]")
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await session.close()
                self.mic.stop()
            if not quitting.is_set():
                self._link(False)
                await asyncio.sleep(0.5)

    async def watchdog(self) -> None:
        down, down_since = False, None
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            down, down_since, event, starved_now = watchdog_tick(
                self.mic.last_frame_t, self.pass_start, down, down_since, self._clock())
            if event == "down":
                self._out("[ears: no mic frames; rebuilding the microphone]")
            elif event == "up":
                self._out("[ears: mic frames back]")
            if starved_now and self.frames_q is not None:
                self.frames_q.put_nowait(_REBUILD)

    async def abandon_ticker(self) -> None:
        while True:
            await asyncio.sleep(ABANDON_INTERVAL_S)
            self.handle_actions(self.machine.abandon_stale())

    # -- the run ---------------------------------------------------------------------

    async def run(self) -> None:
        host = self.host
        host.start()
        self._out(f"[earshot in {host.project_dir}]")
        self._out(f"[say '{self.address} ...' to open a turn and end it with"
                  f" '{self.closer}'; '{self.address} stop / resume / again / back two /"
                  " status / cancel / compact / quit' are local; unaddressed speech"
                  " is ignored]")
        self._tasks = [
            asyncio.ensure_future(self.ears()),
            asyncio.ensure_future(self.watchdog()),
            asyncio.ensure_future(self.abandon_ticker()),
            asyncio.ensure_future(host.still_here_ticker(float(host.cfg["seat"]["still_here_s"]))),
        ]
        ears = self._tasks[0]

        def ears_dead(task):
            if task.cancelled() or task.exception() is None:
                return
            self._out(f"[ears failed: {task.exception()!r}; shutting down]")
            host.quitting.set()

        ears.add_done_callback(ears_dead)
        try:
            await host.quitting.wait()
        finally:
            self._cancel_settle()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await host.shutdown()
            self.mic.stop()
            self.playback.stop()
            self._out("[session closed]")


async def run_voice(host: Host, seat, cfg: dict, keys: dict, *, stt=None, tts=None,
                    playback: Playback | None = None, mic: Mic | None = None) -> None:
    if stt is None:
        from earshot.providers.deepgram import DeepgramSTT
        stt = DeepgramSTT(keys["deepgram"], model=cfg["stt"]["model"])
    if tts is None:
        from earshot.providers.cartesia import CartesiaTTS
        tts = CartesiaTTS(keys["cartesia"], cfg["tts"]["voice"], model=cfg["tts"]["model"])
    if playback is None:
        playback = Playback(enabled=True, device=cfg["audio"].get("output_device"),
                            rate=tts.sample_rate)
        if not playback.enabled:
            print("earshot: no audio output device; run with --text", file=sys.stderr)
            return
    if mic is None:
        mic = Mic(rate=MIC_RATE, device=cfg["audio"].get("input_device"))
    scrubber = Scrubber(cfg["words"]["address"], cfg["words"]["closer"])
    respell = Respeller(cfg.get("respell") or {})
    speak = make_speaker(tts, playback, scrubber, respell, cfg["volumes"],
                         cfg["tts"].get("voice") or None)
    host.bind(seat, SpokenLog(speak))
    front = VoiceFront(host, cfg, stt=stt, tts=tts, playback=playback, mic=mic)
    await front.run()
