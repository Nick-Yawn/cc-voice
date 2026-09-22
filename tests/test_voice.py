"""The voice loop end to end, offline: fake STT events in, a scripted
claude child, a fake TTS, and a playback with a fake stream so earcons
are observable."""

import asyncio

from cc_voice import earcons
from cc_voice.app import Host
from cc_voice.audio import Playback
from cc_voice.config import DEFAULTS, deep_merge
from cc_voice.providers import Error, Final, Partial, SpeechStarted
from cc_voice.providers.fake import FakeSTT, FakeTTS
from cc_voice.seat import Seat
from cc_voice.state import EventLog, LockFile, SessionPin
from cc_voice.voice import run_voice
from tests.fakes import ScriptedClaude, assistant_tool, result, until


class FakeOutStream:
    def __init__(self):
        self.writes = []

    def start(self):
        pass

    def write(self, pcm):
        self.writes.append(pcm)

    def abort(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class FakeMic:
    rate = 16000

    def __init__(self):
        self.last_frame_t = None
        self.started = 0
        self.stopped = 0
        self.on_frame = None

    def start(self, loop, on_frame):
        self.started += 1
        self.on_frame = on_frame

    def stop(self):
        self.stopped += 1


CUES = earcons.get_set()


def cue_names(stream: FakeOutStream) -> list[str]:
    by_bytes = {v: k for k, v in CUES.items()}
    out = []
    for w in stream.writes:
        name = by_bytes.get(w)
        if name:
            out.append(name)
    return out


def build(tmp_path, answers, overlay=None, tts=None):
    cfg = deep_merge(DEFAULTS, {"turns": {"closer_settle_s": 0.03},
                                "volumes": {"earcons": 1.0},
                                "seat": {"still_here_s": 60}, **(overlay or {})})
    out = []
    host = Host(cfg, "/proj", log=EventLog(tmp_path / "log.jsonl"), out=out.append)
    claude = ScriptedClaude(answers)
    pin = SessionPin(tmp_path / "session_id")
    lock = LockFile(tmp_path / "lock")
    lock.acquire(None)
    seat = Seat("/proj", contract_path="/c.md", session_pin=pin,
                on_event=host.on_seat_event, spawn=claude.spawn, env={},
                lock=lock, log=host.log)
    stt = FakeSTT()
    tts = tts or FakeTTS()
    stream = FakeOutStream()
    playback = Playback(enabled=True, open_stream=lambda: stream)
    mic = FakeMic()
    run = asyncio.ensure_future(run_voice(host, seat, cfg, stt=stt, tts=tts,
                                          playback=playback, mic=mic))
    return host, seat, claude, stt, tts, stream, mic, out, run


def test_address_talk_closer_round_trip(tmp_path):
    answers = {"say hi": [assistant_tool("Read", {"file_path": "README.md"}),
                          result("Hi.\n⟦voice⟧Hi there, operator.⟦/voice⟧",
                                 used=1000, window=10000)]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        assert stt.opened_with[0]["keyterms"] == ["operator", "over"]
        await until(lambda: "connected" in cue_names(stream))
        stt.queue.put_nowait(Final("Hi, are you still at church?"))
        stt.queue.put_nowait(Final("Operator, say hi"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        assert "capture" in cue_names(stream)
        stt.queue.put_nowait(Final("over"))
        await until(lambda: "dispatch" in cue_names(stream))
        assert not host.spoken.paused
        await until(lambda: any(t == "10 percent." for t, _ in tts.spoken))
        # scrubbed at the chokepoint: the address word never reaches the voice
        assert [t for t, _ in tts.spoken] == ["Received.", "Reading README.md.",
                                              "Hi there, op.", "10 percent."]
        assert any("ignored: Hi, are you still at church?" in line for line in out)
        assert "you ▸ say hi" in out and "→ sent: say hi" in out
        stt.queue.put_nowait(Final("Operator quit"))
        await run
        assert out[-1] == "[session closed]" and not seat.alive and mic.stopped >= 1

    asyncio.run(scenario())


def test_closer_mid_sentence_is_content_when_speech_continues(tmp_path):
    answers = {"bring that over to the other file": [result("⟦voice⟧Moved.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, bring that over"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        stt.queue.put_nowait(Partial("to the"))  # inside the settle window
        await asyncio.sleep(0.05)
        assert "dispatch" not in cue_names(stream)
        stt.queue.put_nowait(Final("to the other file over"))
        await until(lambda: "dispatch" in cue_names(stream))
        await until(lambda: ("Moved.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_speech_started_cancels_the_settle_and_a_silent_final_rearms(tmp_path):
    answers = {"ship it": [result("⟦voice⟧Shipped.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, ship it over"))
        stt.queue.put_nowait(SpeechStarted())
        await asyncio.sleep(0.05)
        assert "dispatch" not in cue_names(stream)  # the onset held the closer
        stt.queue.put_nowait(Final("   "))  # a silent final re-arms the window
        await until(lambda: "dispatch" in cue_names(stream))
        await until(lambda: ("Shipped.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_talking_pauses_playback_and_commands_drive_the_cursor(tmp_path):
    answers = {"talk": [result("⟦voice⟧A long spoken answer that keeps going.⟦/voice⟧",
                               used=100, window=1000)]}
    tts = FakeTTS(chunk_delay_s=0.02, chunks=50)

    async def scenario():
        host, seat, claude, stt, tts_, stream, mic, out, run = build(tmp_path, answers, tts=tts)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, talk over"))
        await until(lambda: any(t.startswith("A long spoken") for t, _ in tts.spoken))
        # the address word pauses the answer mid-line
        stt.queue.put_nowait(Final("Operator."))
        await until(lambda: tts.cancelled == ["A long spoken answer that keeps going."])
        assert host.spoken.paused
        # cancelling the turn resumes from the start of that line
        stt.queue.put_nowait(Final("Operator cancel"))
        await until(lambda: [t for t, _ in tts.spoken].count(
            "A long spoken answer that keeps going.") == 2)
        assert not host.spoken.paused
        # "operator stop" holds; "operator resume" releases
        stt.queue.put_nowait(Final("um operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        stt.queue.put_nowait(Final("Operator resume"))
        await until(lambda: not host.spoken.paused)
        # "again" and its "repeat" alias replay the ANSWER (through its
        # closer), never the closer alone
        await until(lambda: ("10 percent.", None) in tts.spoken)
        await until(lambda: not host.spoken.busy)
        n = len(tts.spoken)
        stt.queue.put_nowait(Final("Operator again"))
        await until(lambda: len(tts.spoken) == n + 2)
        assert [t for t, _ in tts.spoken[-2:]] == \
            ["A long spoken answer that keeps going.", "10 percent."]
        await until(lambda: not host.spoken.busy)
        stt.queue.put_nowait(Final("Operator repeat"))
        await until(lambda: len(tts.spoken) == n + 4)
        assert tts.spoken[-1] == ("10 percent.", None)
        # status speaks the mic and link clauses
        stt.queue.put_nowait(Final("Operator status"))
        await until(lambda: any(t.startswith("Link up.") for t, _ in tts.spoken))
        status = [t for t, _ in tts.spoken if t.startswith("Link up.")][0]
        assert "No mic frames yet." in status and "Context at 10 percent." in status
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_readdress_after_a_gap_discards_with_the_falling_tone(tmp_path):
    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(
            tmp_path, {}, overlay={"turns": {"readdress_gap_s": 0.05}})
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, half a thought"))
        await until(lambda: host.spoken.holds == frozenset({"talk"}))
        await asyncio.sleep(0.1)
        stt.queue.put_nowait(Final("Operator, fresh"))
        await until(lambda: "abandoned" in cue_names(stream))
        assert any("discarded: half a thought" in line for line in out)
        assert host.spoken.holds == frozenset({"talk"})  # the new turn is open
        stt.queue.put_nowait(Final("Operator cancel"))
        await until(lambda: not host.spoken.paused)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_stt_link_drop_reconnects_with_the_cue_pair(tmp_path):
    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, {})
        await until(lambda: len(stt.sessions) == 1)
        stt.queue.put_nowait(Error("link: server closed 1011"))
        await until(lambda: len(stt.sessions) == 2)
        await until(lambda: cue_names(stream).count("connected") == 2)
        assert cue_names(stream).count("disconnected") == 1
        assert mic.started == 2 and mic.stopped >= 1
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_compact_by_voice_writes_the_slash_command(tmp_path):
    answers = {"one": [result("⟦voice⟧One.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, one over"))
        await until(lambda: ("One.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator compact"))
        await until(lambda: seat.compact_pending)
        assert claude.procs[0].stdin.writes[-1].endswith('"/compact"}]}}\n')
        await until(lambda: ("Compacting.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_a_stop_holds_against_new_output_but_ends_on_new_input(tmp_path):
    answers = {"first": [result("⟦voice⟧First answer.⟦/voice⟧", used=100, window=1000)],
               "second": [result("⟦voice⟧Second answer.⟦/voice⟧", used=200, window=1000)]}
    tts = FakeTTS(chunk_delay_s=0.02, chunks=8)

    async def scenario():
        host, seat, claude, stt, tts_, stream, mic, out, run = build(tmp_path, answers, tts=tts)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, first over"))
        await until(lambda: ("Received.", None) in tts.spoken)
        stt.queue.put_nowait(Final("Operator stop"))  # while "Received." plays
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        # the answer arrives while stopped: it queues silently
        await until(lambda: any(e.text == "First answer." for e in host.spoken.entries))
        await asyncio.sleep(0.1)
        assert not any(t == "First answer." for t, _ in tts.spoken)
        assert host.spoken.paused
        # "resume" plays the stopped line and its backlog
        stt.queue.put_nowait(Final("Operator resume"))
        await until(lambda: ("10 percent.", None) in tts.spoken)
        assert [t for t, _ in tts.spoken].count("Received.") == 2
        assert ("First answer.", None) in tts.spoken
        await until(lambda: not host.spoken.busy)
        # a stop, then new INPUT: the stopped line is abandoned, new content plays
        stt.queue.put_nowait(Final("Operator, second over"))
        await until(lambda: [t for t, _ in tts.spoken].count("Received.") == 3)
        stt.queue.put_nowait(Final("Operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        await until(lambda: any(e.text == "Second answer." for e in host.spoken.entries))
        n = len(tts.spoken)
        stt.queue.put_nowait(Final("Operator status"))  # any command is input
        await until(lambda: any(t.startswith("Link up.") for t, _ in tts.spoken))
        assert not host.spoken.paused
        assert "Second answer." not in [t for t, _ in tts.spoken[n:]]  # abandoned
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())


def test_compact_after_a_stop_is_heard(tmp_path):
    answers = {"one": [result("⟦voice⟧One.⟦/voice⟧")]}

    async def scenario():
        host, seat, claude, stt, tts, stream, mic, out, run = build(tmp_path, answers)
        await until(lambda: stt.sessions)
        stt.queue.put_nowait(Final("Operator, one over"))
        await until(lambda: ("One.", None) in tts.spoken)
        await until(lambda: not host.spoken.busy)
        stt.queue.put_nowait(Final("Operator stop"))
        await until(lambda: host.spoken.holds == frozenset({"user"}))
        stt.queue.put_nowait(Final("Operator compact"))  # the live lock-up: a
        await until(lambda: seat.compact_pending)         # command clears the stop
        await until(lambda: ("Compacting.", None) in tts.spoken)
        assert [t for t, _ in tts.spoken][-2:] == ["Received.", "Compacting."]
        assert not host.spoken.paused
        stt.queue.put_nowait(Final("Operator quit"))
        await run

    asyncio.run(scenario())
