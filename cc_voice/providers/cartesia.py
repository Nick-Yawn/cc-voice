"""Cartesia text to speech (Sonic).

wss://api.cartesia.ai/tts/websocket: sentence-sized sends under one
context (continue: true, then an empty transcript with continue: false
to flush), base64 PCM chunks back until `done`. One websocket per
utterance; the context carries prosody across the sentence seams.
There is no speed field: cc-voice has no speed control.
"""

import base64
import contextlib
import json
import uuid

from cc_voice.providers import TTSCaps
from cc_voice.text import sentence_chunks

TTS_URL = "wss://api.cartesia.ai/tts/websocket"
API_VERSION = "2026-08-14"
DEFAULT_MODEL = "sonic-3.6-2026-08-27"
SAMPLE_RATE = 24000


def request_json(model: str, voice_id: str, context_id: str, transcript: str,
                 cont: bool, language: str = "en", sample_rate: int = SAMPLE_RATE) -> str:
    return json.dumps({
        "model_id": model,
        "transcript": transcript,
        "voice": {"id": voice_id},
        "output_format": {"container": "raw", "encoding": "pcm_s16le",
                          "sample_rate": sample_rate},
        "language": language,
        "context_id": context_id,
        "continue": cont,
    })


def parse_message(msg: dict, context_id: str):
    """("chunk", bytes) | ("done", None) | ("error", message) | None."""
    if not isinstance(msg, dict):
        return None
    if msg.get("context_id") not in (None, context_id):
        return None
    kind = msg.get("type")
    if kind == "chunk" and msg.get("data"):
        return ("chunk", base64.b64decode(msg["data"]))
    if kind == "done":
        return ("done", None)
    if kind == "error":
        return ("error", str(msg.get("message") or msg))
    return None


class CartesiaTTS:
    caps = TTSCaps(streaming=True, pauses=False, word_timestamps=False,
                   pronunciation=False)
    sample_rate = SAMPLE_RATE

    def __init__(self, api_key: str, voice_id: str, model: str = DEFAULT_MODEL,
                 language: str = "en", connect=None):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.language = language
        self._connect = connect or self._websocket

    @staticmethod
    def _websocket(url: str, headers: dict):
        import websockets
        return websockets.connect(url, additional_headers=headers)

    async def speak(self, text: str, *, voice: str | None = None):
        voice_id = voice or self.voice_id
        context_id = uuid.uuid4().hex
        chunks = sentence_chunks(text)
        if not chunks:
            return
        url = f"{TTS_URL}?cartesia_version={API_VERSION}"
        async with self._connect(url, {"X-API-Key": self.api_key}) as ws:
            for chunk in chunks:
                await ws.send(request_json(self.model, voice_id, context_id, chunk,
                                           True, self.language, self.sample_rate))
            await ws.send(request_json(self.model, voice_id, context_id, "",
                                       False, self.language, self.sample_rate))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                parsed = parse_message(msg, context_id)
                if parsed is None:
                    continue
                kind, payload = parsed
                if kind == "chunk":
                    yield payload
                elif kind == "done":
                    return
                else:
                    raise RuntimeError(f"cartesia: {payload}")


def aclosing(gen):
    return contextlib.aclosing(gen)
