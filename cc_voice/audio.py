"""Audio devices through sounddevice: the speaker and the microphone.

Playback: play() only enqueues; a dedicated daemon thread does the
blocking stream.write() at real-time pace, off the event loop (an
inline write once stalled the loop long enough to kill two unrelated
websockets at once). abort() drops everything queued and restarts the
stream. A marker enqueued behind audio resolves a future when the drain
thread reaches it, which is how the player awaits "played out".

Mic: a RawInputStream whose callback hands PCM16 frames to the loop.
The frame-starvation watchdog (watchdog_tick, pure) notices a wedged or
changed device long before the STT vendor's own keepalive would.
"""

import asyncio
import queue
import sys
import threading
import time
from array import array

OUT_RATE = 24_000
MIC_RATE = 16_000
MIC_CHUNK_MS = 40

MIC_STARVED_S = 5.0


def scale_pcm(pcm: bytes, gain: float) -> bytes:
    """Amplitude-scale a frame-aligned PCM16 mono buffer, clamped. An
    odd-length buffer passes through untouched rather than raising."""
    if gain == 1.0 or len(pcm) % 2:
        return pcm
    samples = array("h", pcm)
    return array("h", (max(-32768, min(32767, int(v * gain)))
                       for v in samples)).tobytes()


class Playback:
    _STOP = object()

    def __init__(self, enabled: bool = True, device=None, rate: int = OUT_RATE,
                 open_stream=None):
        self.rate = rate
        self.device = device
        self._open = open_stream or self._raw_output
        self.stream = None
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._thread: threading.Thread | None = None
        self._markers: list = []  # (loop, future) not yet reached
        if not enabled:
            return
        try:
            self.stream = self._open()
            self.stream.start()
        except Exception as exc:
            print(f"[no audio output: {exc}]", file=sys.stderr)
            return
        self._start_thread()

    def _raw_output(self):
        import sounddevice as sd
        return sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                  device=self.device)

    @property
    def enabled(self) -> bool:
        return self._thread is not None

    def _start_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._drain, daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is self._STOP:
                return
            if isinstance(item, tuple):  # a marker: (loop, future)
                self._resolve(item)
                continue
            with self._lock:
                stream, gen = self.stream, self._gen
            if stream is None:
                continue
            try:
                stream.write(item)
            except Exception as exc:
                with self._lock:
                    caused_by_abort = self._gen != gen
                if not caused_by_abort:
                    print(f"[playback write failed: {exc}]", file=sys.stderr)

    @staticmethod
    def _resolve(marker) -> None:
        loop, fut = marker
        try:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
        except RuntimeError:
            pass  # the loop is closed

    def play(self, pcm: bytes, gain: float = 1.0) -> None:
        if self._thread is None or not pcm:
            return
        self._q.put(scale_pcm(pcm, gain))

    def marker(self) -> "asyncio.Future":
        """A future resolved when everything queued before it has been
        written to the device (resolved at once with no device)."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if self._thread is None:
            fut.set_result(None)
            return fut
        self._q.put((loop, fut))
        return fut

    async def wait_played(self) -> None:
        await self.marker()

    def _drop_queued(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if isinstance(item, tuple):
                    self._resolve(item)
        except queue.Empty:
            pass

    def abort(self) -> None:
        """Drop everything queued and cut the audio in flight."""
        with self._lock:
            self._gen += 1
            stream = self.stream
        self._drop_queued()
        if stream is None:
            return
        try:
            stream.abort()
        except Exception as exc:
            print(f"[playback abort failed: {exc}]", file=sys.stderr)
        try:
            stream.start()
        except Exception as exc:
            print(f"[playback restart failed: {exc}; muting]", file=sys.stderr)
            with self._lock:
                if self.stream is stream:
                    self.stream = None

    def stop(self) -> None:
        thread = self._thread
        if thread is not None:
            with self._lock:
                self._gen += 1
            self._drop_queued()
            self._q.put(self._STOP)
            if self.stream is not None:
                try:
                    self.stream.abort()
                except Exception:
                    pass
            thread.join(timeout=2.0)
            self._thread = None
        stream, self.stream = self.stream, None
        if stream is not None:
            for op in (stream.stop, stream.close):
                try:
                    op()
                except Exception:
                    pass


class Mic:
    """The input device: frames reach on_frame(bytes) on the loop thread.
    last_frame_t is written only by a real callback."""

    def __init__(self, rate: int = MIC_RATE, chunk_ms: int = MIC_CHUNK_MS, device=None,
                 open_stream=None):
        self.rate = rate
        self.chunk_ms = chunk_ms
        self.device = device
        self._open = open_stream or self._raw_input
        self.stream = None
        self.last_frame_t: float | None = None

    def _raw_input(self, callback):
        import sounddevice as sd
        return sd.RawInputStream(samplerate=self.rate, channels=1, dtype="int16",
                                 blocksize=self.rate * self.chunk_ms // 1000,
                                 device=self.device, callback=callback)

    def start(self, loop, on_frame) -> None:
        def callback(indata, frames, t, status):
            self.last_frame_t = time.monotonic()
            loop.call_soon_threadsafe(on_frame, bytes(indata))

        self.stream = self._open(callback)
        self.stream.start()

    def stop(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            for op in (stream.stop, stream.close):
                try:
                    op()
                except Exception:
                    pass


def starved(last_frame_t: float | None, now: float, threshold: float = MIC_STARVED_S) -> bool:
    return last_frame_t is not None and (now - last_frame_t) > threshold


def watchdog_tick(last_frame_t: float | None, pass_start: float, down: bool,
                  down_since: float | None, now: float,
                  threshold: float = MIC_STARVED_S) -> tuple[bool, float | None, str | None, bool]:
    """One tick of the mic-starvation watchdog, pure. Returns (down,
    down_since, event, starved_now): event is "down" on the falling
    edge, "up" once a REAL frame newer than the moment we went down has
    arrived, else None; starved_now is the rebuild trigger. The
    reference clock is the newer of the last real frame and the pass
    start, so a fresh pass gets its grace window without ever faking a
    frame."""
    reference = pass_start if last_frame_t is None or pass_start > last_frame_t \
        else last_frame_t
    if starved(reference, now, threshold):
        if not down:
            return True, now, "down", True
        return True, down_since, None, True
    if down:
        if last_frame_t is not None and down_since is not None and last_frame_t > down_since:
            return False, None, "up", False
        return True, down_since, None, False
    return False, None, None, False
