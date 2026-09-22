"""Earcons: machinery cues, never narration. Earcons for machinery,
voice for content.

Pure PCM16 mono synthesis, no audio deps. A marimba-ish family:
fundamental plus a quiet octave overtone, quick percussive decay.

  capture      a soft tick: the address word opened a turn
  dispatch     a rising two-note run: the turn is on its way
  abandoned    a falling two-note figure: an open turn was discarded
  still_here   a soft low blip: Claude is still working
  connected    a triple rising triad: mic and link are up
  disconnected one long low note: the link dropped
"""

import math
from array import array

SAMPLE_RATE = 24_000

CUE_KEYS = ("capture", "dispatch", "abandoned", "still_here", "connected", "disconnected")


def _note(freq: float, dur_s: float, sample_rate: int = SAMPLE_RATE,
          amp: int = 9000) -> array:
    n = max(1, int(sample_rate * dur_s))
    out = array("h")
    for i in range(n):
        t = i / sample_rate
        decay = math.exp(-6.0 * t / dur_s)
        attack = min(1.0, i / 30)
        s = (math.sin(2 * math.pi * freq * i / sample_rate)
             + 0.4 * math.sin(2 * math.pi * 2 * freq * i / sample_rate))
        out.append(int(amp * 0.7 * attack * decay * s))
    return out


def _gap(dur_s: float, sample_rate: int = SAMPLE_RATE) -> array:
    return array("h", [0] * int(sample_rate * dur_s))


def _blip(sample_rate: int = SAMPLE_RATE) -> array:
    """A soft, low two-note blip: still here, still thinking. Gentler
    and lower than the others; ambient, not attention-seeking."""
    out = array("h")
    for freq, dur in ((392.0, 0.09), (329.6, 0.11)):
        n = int(sample_rate * dur)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400)
            out.append(int(3500 * env * math.sin(2 * math.pi * freq * i / sample_rate)))
    return out


def _bytes(*parts: array) -> bytes:
    out = array("h")
    for part in parts:
        out.extend(part)
    return out.tobytes()


def get_set(sample_rate: int = SAMPLE_RATE) -> dict[str, bytes]:
    return {
        "capture": _bytes(_note(523.25, 0.12, sample_rate)),                     # C5
        "dispatch": _bytes(_note(523.25, 0.08, sample_rate), _gap(0.02, sample_rate),
                           _note(659.25, 0.12, sample_rate)),                    # C5 -> E5
        "abandoned": _bytes(_note(392.00, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(293.66, 0.22, sample_rate)),                   # G4 -> D4
        "still_here": _bytes(_blip(sample_rate)),
        "connected": _bytes(_note(523.25, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(659.25, 0.10, sample_rate), _gap(0.02, sample_rate),
                            _note(783.99, 0.18, sample_rate)),                   # C5 E5 G5
        "disconnected": _bytes(_note(196.00, 0.60, sample_rate)),                # G3, long
    }
