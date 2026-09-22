"""Deepgram streaming speech to text (Nova-3).

wss://api.deepgram.com/v1/listen: linear16 PCM in, `Results` JSON out.
`keyterm` params boost the address and closer words (Nova-3 only); the
address word became reliable only once it was boosted. `vad_events`
gives a SpeechStarted message at speech onset, the earliest signal that
a pending closer is being talked over.
"""

import asyncio
import json
import urllib.parse

from earshot.providers import (
    Error,
    Final,
    Partial,
    SpeechStarted,
    STTCaps,
    Word,
)

LISTEN_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"


def listen_params(model: str, rate: int, keyterms=(), language: str = "en") -> list[tuple]:
    params = [
        ("model", model),
        ("encoding", "linear16"),
        ("sample_rate", rate),
        ("channels", 1),
        ("language", language),
        ("interim_results", "true"),
        ("punctuate", "true"),
        ("smart_format", "true"),
        ("vad_events", "true"),
    ]
    params += [("keyterm", k) for k in keyterms if k]
    return params


def listen_url(model: str, rate: int, keyterms=(), language: str = "en") -> str:
    return f"{LISTEN_URL}?{urllib.parse.urlencode(listen_params(model, rate, keyterms, language))}"


def parse_message(msg: dict):
    """One Deepgram message -> an STT event, or None for the rest
    (Metadata, UtteranceEnd, empty partials)."""
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "SpeechStarted":
        return SpeechStarted()
    if kind == "Error":
        return Error(str(msg.get("message") or msg.get("description") or msg))
    if kind != "Results":
        return None
    alts = (msg.get("channel") or {}).get("alternatives") or [{}]
    alt = alts[0] if isinstance(alts[0], dict) else {}
    text = (alt.get("transcript") or "").strip()
    if not msg.get("is_final"):
        return Partial(text) if text else None
    # An empty final is still a final: silence ended a segment, which the
    # core uses to re-arm a pending closer's window.
    words = None
    raw_words = alt.get("words")
    if isinstance(raw_words, list):
        words = tuple(
            Word(w.get("punctuated_word") or w.get("word") or "",
                 float(w.get("start") or 0.0), float(w.get("end") or 0.0),
                 float(w.get("confidence") or 0.0))
            for w in raw_words if isinstance(w, dict))
    return Final(text, words, bool(msg.get("speech_final")))


class DeepgramSession:
    def __init__(self, ws):
        self._ws = ws
        self._closed = False

    async def send(self, pcm16: bytes) -> None:
        await self._ws.send(pcm16)

    async def events(self):
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                ev = parse_message(msg)
                if ev is not None:
                    yield ev
        except Exception as exc:  # a closed or broken link: the caller reconnects
            if not self._closed:
                yield Error(f"deepgram link: {exc!r}")

    async def close(self) -> None:
        self._closed = True
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        try:
            await self._ws.close()
        except Exception:
            pass


class DeepgramSTT:
    caps = STTCaps(partials=True, word_timings=True, native_turns=False,
                   keyterms=True, streaming=True)

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, connect=None):
        self.api_key = api_key
        self.model = model
        self._connect = connect or self._websocket

    @staticmethod
    async def _websocket(url: str, headers: dict):
        import websockets
        return await websockets.connect(url, additional_headers=headers)

    async def open(self, rate: int = 16000, keyterms=(), language: str = "en") -> DeepgramSession:
        url = listen_url(self.model, rate, keyterms, language)
        ws = await self._connect(url, {"Authorization": f"Token {self.api_key}"})
        return DeepgramSession(ws)
