"""The Deepgram and Cartesia adapters against fake websockets, and the
pure builders and parsers around them."""

import asyncio
import base64
import json
import urllib.parse

import pytest

from cc_voice.providers import Error, Final, Partial, SpeechStarted, STT, TTS
from cc_voice.providers.cartesia import CartesiaTTS, parse_message as parse_cartesia, request_json
from cc_voice.providers.deepgram import DeepgramSTT, listen_url, parse_message as parse_deepgram
from cc_voice.providers.fake import FakeSTT, FakeTTS


class FakeWS:
    """Both vendors' websockets: send() records, iteration yields scripted
    messages, close() records. Usable as an async context manager and as
    an awaitable (the two shapes the adapters use)."""

    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = list(incoming)
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __await__(self):
        async def me():
            return self
        return me().__await__()


# -- Deepgram -------------------------------------------------------------

def test_listen_url_carries_model_encoding_and_keyterms():
    url = listen_url("nova-3", 16000, ["operator", "over"], "en")
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["model"] == ["nova-3"] and q["encoding"] == ["linear16"]
    assert q["sample_rate"] == ["16000"] and q["channels"] == ["1"]
    assert q["interim_results"] == ["true"] and q["vad_events"] == ["true"]
    assert q["keyterm"] == ["operator", "over"]
    assert "keyterm" not in urllib.parse.parse_qs(
        urllib.parse.urlparse(listen_url("nova-3", 16000, [], "en")).query)


def test_parse_deepgram_messages():
    assert parse_deepgram({"type": "Metadata"}) is None
    assert parse_deepgram({"type": "SpeechStarted"}) == SpeechStarted()
    assert parse_deepgram({"type": "Error", "message": "bad key"}) == Error("bad key")
    empty_partial = {"type": "Results", "is_final": False,
                     "channel": {"alternatives": [{"transcript": "   "}]}}
    assert parse_deepgram(empty_partial) is None
    empty_final = {"type": "Results", "is_final": True, "speech_final": True,
                   "channel": {"alternatives": [{"transcript": ""}]}}
    assert parse_deepgram(empty_final) == Final("", None, True)  # silence is news
    partial = {"type": "Results", "is_final": False,
               "channel": {"alternatives": [{"transcript": "operator run"}]}}
    assert parse_deepgram(partial) == Partial("operator run")
    final = {"type": "Results", "is_final": True, "speech_final": True,
             "channel": {"alternatives": [{"transcript": "Operator, run it. Over.",
                                           "words": [
                                               {"word": "operator", "punctuated_word": "Operator,",
                                                "start": 0.1, "end": 0.5, "confidence": 0.99},
                                               {"word": "over", "start": 1.0, "end": 1.3,
                                                "confidence": 0.9}]}]}}
    ev = parse_deepgram(final)
    assert isinstance(ev, Final) and ev.text == "Operator, run it. Over." and ev.speech_final
    assert [w.text for w in ev.words] == ["Operator,", "over"]
    assert ev.words[1].start_s == 1.0 and ev.words[1].end_s == 1.3
    assert parse_deepgram(["not", "a", "dict"]) is None


def test_deepgram_session_streams_events_and_closes():
    async def scenario():
        ws = FakeWS([
            json.dumps({"type": "Results", "is_final": False,
                        "channel": {"alternatives": [{"transcript": "hel"}]}}),
            b"binary noise",
            "not json",
            json.dumps({"type": "Results", "is_final": True,
                        "channel": {"alternatives": [{"transcript": "hello"}]}}),
        ])
        seen = {}

        async def connect(url, headers):
            seen["url"], seen["headers"] = url, headers
            return ws

        stt = DeepgramSTT("dg-key", connect=connect)
        assert isinstance(stt, STT)
        session = await stt.open(rate=16000, keyterms=["operator", "over"])
        assert seen["headers"] == {"Authorization": "Token dg-key"}
        assert "keyterm=operator" in seen["url"]
        await session.send(b"\x00\x00")
        assert ws.sent == [b"\x00\x00"]
        events = [ev async for ev in session.events()]
        assert events == [Partial("hel"), Final("hello", None, False)]
        await session.close()
        assert ws.closed and json.loads(ws.sent[-1]) == {"type": "CloseStream"}

    asyncio.run(scenario())


def test_deepgram_link_failure_surfaces_as_an_error_event():
    async def scenario():
        ws = FakeWS([RuntimeError("1011 keepalive timeout")])
        stt = DeepgramSTT("k", connect=lambda url, headers: ws)
        session = await stt.open()
        events = [ev async for ev in session.events()]
        assert len(events) == 1 and isinstance(events[0], Error)
        assert "1011" in events[0].message

    asyncio.run(scenario())


# -- Cartesia -------------------------------------------------------------

def test_request_json_shape_has_no_speed_field():
    req = json.loads(request_json("sonic-3.6-2026-08-27", "voice-1", "ctx", "Hello.", True))
    assert req == {
        "model_id": "sonic-3.6-2026-08-27", "transcript": "Hello.",
        "voice": {"id": "voice-1"},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
        "language": "en", "context_id": "ctx", "continue": True}
    assert "generation_config" not in req and "speed" not in req
    assert json.loads(request_json("m", "v", "ctx", "", False))["continue"] is False


def test_parse_cartesia_messages():
    data = base64.b64encode(b"\x01\x02").decode()
    assert parse_cartesia({"type": "chunk", "data": data, "context_id": "c"}, "c") == \
        ("chunk", b"\x01\x02")
    assert parse_cartesia({"type": "chunk", "data": data, "context_id": "other"}, "c") is None
    assert parse_cartesia({"type": "done"}, "c") == ("done", None)
    assert parse_cartesia({"type": "error", "message": "voice not found"}, "c") == \
        ("error", "voice not found")
    assert parse_cartesia({"type": "timestamps"}, "c") is None


def test_cartesia_speak_sends_sentences_then_flush_and_yields_pcm():
    async def scenario():
        pcm1, pcm2 = b"\x00\x01" * 10, b"\x02\x03" * 10
        ws = FakeWS()
        seen = {}

        def connect(url, headers):
            seen["url"], seen["headers"] = url, headers
            ws.incoming = [
                json.dumps({"type": "chunk", "data": base64.b64encode(pcm1).decode()}),
                json.dumps({"type": "chunk", "data": base64.b64encode(pcm2).decode()}),
                json.dumps({"type": "done"}),
                json.dumps({"type": "chunk", "data": "never read"}),
            ]
            return ws

        tts = CartesiaTTS("ck", "voice-1", connect=connect)
        assert isinstance(tts, TTS) and tts.sample_rate == 24000
        out = [pcm async for pcm in tts.speak("First one. Second one!", voice=None)]
        assert out == [pcm1, pcm2]
        assert seen["headers"] == {"X-API-Key": "ck"}
        assert seen["url"] == "wss://api.cartesia.ai/tts/websocket?cartesia_version=2026-08-14"
        reqs = [json.loads(s) for s in ws.sent]
        assert [(r["transcript"], r["continue"]) for r in reqs] == \
            [("First one.", True), ("Second one!", True), ("", False)]
        assert len({r["context_id"] for r in reqs}) == 1
        assert all(r["voice"]["id"] == "voice-1" for r in reqs)
        assert ws.closed
        # an explicit voice overrides the default; empty text sends nothing
        ws2 = FakeWS([json.dumps({"type": "done"})])
        tts2 = CartesiaTTS("ck", "voice-1", connect=lambda u, h: ws2)
        assert [p async for p in tts2.speak("Hi.", voice="voice-2")] == []
        assert json.loads(ws2.sent[0])["voice"]["id"] == "voice-2"
        assert [p async for p in tts2.speak("   ")] == []

    asyncio.run(scenario())


def test_cartesia_error_raises():
    async def scenario():
        ws = FakeWS([json.dumps({"type": "error", "message": "voice not found"})])
        tts = CartesiaTTS("ck", "v", connect=lambda u, h: ws)
        with pytest.raises(RuntimeError, match="voice not found"):
            async for _ in tts.speak("Hello."):
                pass
        assert ws.closed

    asyncio.run(scenario())


def test_cartesia_cancel_mid_stream_closes_the_socket():
    async def scenario():
        ws = FakeWS([json.dumps({"type": "chunk", "data": base64.b64encode(b"\x00\x00").decode()})
                     for _ in range(5)] + [json.dumps({"type": "done"})])
        tts = CartesiaTTS("ck", "v", connect=lambda u, h: ws)
        import contextlib
        async with contextlib.aclosing(tts.speak("Hello there.")) as chunks:
            async for _ in chunks:
                break  # the consumer stops iterating: cancel
        assert ws.closed

    asyncio.run(scenario())


# -- the fakes honor the interface -----------------------------------------

def test_fakes_are_providers():
    async def scenario():
        stt = FakeSTT()
        assert isinstance(stt, STT)
        session = await stt.open(keyterms=["operator"])
        stt.queue.put_nowait(Final("hi"))
        stt.queue.put_nowait(None)
        assert [ev async for ev in session.events()] == [Final("hi")]
        assert stt.opened_with[0]["keyterms"] == ["operator"]
        tts = FakeTTS()
        assert isinstance(tts, TTS)
        chunks = [c async for c in tts.speak("Hello.", voice="v")]
        assert len(chunks) == 2 and tts.spoken == [("Hello.", "v")]

    asyncio.run(scenario())
