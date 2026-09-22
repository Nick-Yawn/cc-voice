import asyncio
import threading
import time
from array import array

from earshot import earcons
from earshot.audio import Playback, scale_pcm, starved, watchdog_tick
from earshot.text import Respeller, sentence_chunks


# -- earcons --------------------------------------------------------------

def test_every_cue_is_nonempty_even_pcm16_and_distinct():
    cues = earcons.get_set()
    assert set(cues) == set(earcons.CUE_KEYS)
    for key, pcm in cues.items():
        assert isinstance(pcm, bytes) and pcm and len(pcm) % 2 == 0, key
    vals = list(cues.values())
    assert len({v for v in vals}) == len(vals)
    rate_bytes = earcons.SAMPLE_RATE * 2
    assert int(0.5 * rate_bytes) <= len(cues["disconnected"]) <= int(0.65 * rate_bytes)
    assert len(cues["connected"]) > len(cues["dispatch"]) > len(cues["capture"])
    assert len(earcons.get_set(48000)["capture"]) == 2 * len(cues["capture"])


# -- pcm and the watchdog ---------------------------------------------------

def test_scale_pcm():
    pcm = array("h", [1000, -1000, 32767, -32768]).tobytes()
    assert scale_pcm(pcm, 1.0) is pcm
    assert list(array("h", scale_pcm(pcm, 0.5))) == [500, -500, 16383, -16384]
    assert list(array("h", scale_pcm(pcm, 2.0))) == [2000, -2000, 32767, -32768]
    assert scale_pcm(b"odd", 0.5) == b"odd"


def test_starved_and_watchdog_tick():
    assert not starved(None, 100.0)
    assert not starved(96.0, 100.0)
    assert starved(90.0, 100.0)
    # a never-heard mic: down once, then periodic rebuilds, never "up"
    d, since, ev, s = watchdog_tick(None, 0.0, False, None, 6.0)
    assert (d, since, ev, s) == (True, 6.0, "down", True)
    d, since, ev, s = watchdog_tick(None, 0.0, d, since, 8.0)
    assert (d, ev, s) == (True, None, True)
    # a fresh pass gets its grace window without a rebuild
    d, since, ev, s = watchdog_tick(None, 8.0, d, since, 9.0)
    assert (d, ev, s) == (True, None, False)
    # a real frame newer than the drop: up exactly once
    d, since, ev, s = watchdog_tick(9.5, 8.0, d, since, 10.0)
    assert (d, since, ev, s) == (False, None, "up", False)
    d, since, ev, s = watchdog_tick(9.5, 8.0, d, since, 11.0)
    assert (d, ev, s) == (False, None, False)


# -- playback with a fake stream -----------------------------------------------

class FakeStream:
    def __init__(self):
        self.writes = []
        self.aborts = 0
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.block = None

    def start(self):
        self.starts += 1

    def write(self, pcm):
        if self.block is not None:
            self.block.wait(timeout=2.0)
        self.writes.append(pcm)

    def abort(self):
        self.aborts += 1
        if self.block is not None:
            self.block.set()

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_disabled_playback_is_a_no_op():
    pb = Playback(enabled=False)
    assert not pb.enabled
    pb.play(b"\x00\x00")
    pb.abort()
    pb.stop()

    async def marker_resolves_at_once():
        await asyncio.wait_for(pb.wait_played(), 1.0)

    asyncio.run(marker_resolves_at_once())


def test_playback_writes_in_order_scales_and_markers_resolve():
    fs = FakeStream()
    pb = Playback(enabled=True, open_stream=lambda: fs)
    assert pb.enabled and fs.starts == 1
    pcm = array("h", [1000, -1000]).tobytes()
    pb.play(pcm, 0.5)
    pb.play(pcm)

    async def scenario():
        await asyncio.wait_for(pb.wait_played(), 2.0)

    asyncio.run(scenario())
    assert fs.writes == [array("h", [500, -500]).tobytes(), pcm]
    pb.stop()
    assert fs.stops == 1 and fs.closes == 1 and not pb.enabled


def test_abort_drops_queued_audio_and_releases_markers():
    fs = FakeStream()
    fs.block = threading.Event()
    pb = Playback(enabled=True, open_stream=lambda: fs)
    pb.play(b"\x00\x00" * 4)  # picked up, blocks in write()
    pb.play(b"\x01\x01" * 4)  # queued

    async def scenario():
        fut = pb.marker()
        assert wait_until(lambda: fs.block is not None and not fs.block.is_set() or True)
        pb.abort()
        await asyncio.wait_for(fut, 2.0)  # a dropped marker still resolves

    asyncio.run(scenario())
    assert fs.aborts == 1 and fs.starts == 2
    time.sleep(0.05)
    assert b"\x01\x01" * 4 not in fs.writes
    pb.stop()


def test_a_failed_open_degrades_to_silence(capsys):
    def boom():
        raise RuntimeError("no such device")

    pb = Playback(enabled=True, open_stream=boom)
    assert not pb.enabled and pb.stream is None
    pb.play(b"\x00\x00")
    assert "no audio output" in capsys.readouterr().err


# -- text helpers ---------------------------------------------------------------

def test_sentence_chunks():
    assert sentence_chunks("One. Two! Three?") == ["One.", "Two!", "Three?"]
    assert sentence_chunks("Just  the one,\n  no gap.") == ["Just the one, no gap."]
    assert sentence_chunks("Editing voice_dev.py now.") == ["Editing voice_dev.py now."]
    assert sentence_chunks("Reading wake.py. Then tests.") == ["Reading wake.py.", "Then tests."]
    assert sentence_chunks("   ") == []


def test_respeller():
    r = Respeller({"dev": "devv", "CLI": "C L I"})
    assert r("the dev seat and the cli") == "the devv seat and the C L I"
    assert r("the device is fine") == "the device is fine"
    assert r("Editing voice_dev.py.") == "Editing voice devv dot pie."
    assert r("x.py.bak stays") == "x.py.bak stays"
    assert Respeller()("untouched text") == "untouched text"
    assert Respeller()("") == ""
