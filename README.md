# cc-voice

A voice interface for Claude Code. You talk to your coding agent, and it
talks back.

One key: Cartesia does both the listening and the speaking.

Run `cc-voice` in a project folder. Say the address word ("operator"),
talk, and end with "over". Your words go to your own installed `claude`
as a message. The full answer appears in the terminal, and a short
spoken summary is read aloud, followed by the context-window fill
("32 percent."), which is how you know the turn is over.

Status: the first usable version. Text and voice both work end to end;
voice permission prompts, interrupting a running query, and `cc-voice
setup` are not built yet. See `docs/design.md` for the design and its
open questions.

## What you need

- macOS (Linux should work; Windows is not supported yet).
- Python 3.11 or newer.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed
  and logged in. cc-voice never handles the login; it drives the `claude`
  binary on your PATH under your own account.
- Headphones. cc-voice scrubs its own control words from everything it
  speaks, but on open speakers the mic still hears the voice.
- A microphone. A wired headset or the machine's built-in mic is the
  most reliable setup. Bluetooth headset mics are supported but
  flakier: the headset renegotiates its profile (and the mic's sample
  rate) whenever playback starts or stops, and a dead link tends to
  deliver silence instead of stopping. cc-voice opens the device at
  its own rate, watches for frames that stop or go silent, and
  rebuilds the input when they do; the log names the device it opened.
- For voice mode, one API key: [Cartesia](https://cartesia.ai)
  (`CARTESIA_API_KEY`) does speech to text (Ink) and text to speech
  (Sonic). You also need the id of a Cartesia voice (pick one in their
  playground and copy its id).
- Optionally, [Deepgram](https://deepgram.com) for speech to text
  instead (`[stt] provider = "deepgram"` in the config and
  `DEEPGRAM_API_KEY`). Deepgram Nova-3 sends partial transcripts and
  word timings; Cartesia Ink-2 sends finals only.

Text mode needs no keys and no audio device.

## Install

From a checkout:

```sh
git clone git@github.com:Nick-Yawn/cc-voice.git
cd cc-voice
python3 -m venv .venv && .venv/bin/pip install -e .
```

or with [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e .`

That puts a `cc-voice` command in the venv (`.venv/bin/cc-voice`). The
audio layer uses `sounddevice`, which needs PortAudio; on macOS the
wheel bundles it. On Linux, `apt install libportaudio2` first.

## Configure

Create `~/.config/cc-voice/config.toml`. Only the voice id is required:

```toml
[tts]
voice = "your-cartesia-voice-id"

[stt]
provider = "cartesia"  # or "deepgram"

[words]
address = "operator"   # the word that opens a turn
closer = "over"        # the word that sends it

[volumes]
speech = 1.0           # what Claude says to you
narration = 0.5        # tool-call narration, quieter by design
earcons = 0.6

[gate]
hangover_s = 10.0      # quiet after your last words before the speech link closes

[seat]
# extra flags for the claude child, e.g. to pre-approve edits:
# claude_args = ["--permission-mode", "acceptEdits"]

[respell]
# how the voice should say jargon it mangles
# dev = "devv"
```

A project can override any of it with a `.cc-voice.toml` in its folder.
Keys come from the environment, never the config file:

```sh
export CARTESIA_API_KEY=...
export DEEPGRAM_API_KEY=...   # only with provider = "deepgram"
```

At startup cc-voice names exactly the key the chosen providers need
and is missing. It removes every speech key from the child claude's
environment.

## Run

Try it without audio first:

```sh
cd your-project
cc-voice --text
```

Type a message and press Enter. You'll see claude start, the message go
out, tool calls narrated as they happen, the full answer, and then the
lines that would be spoken (marked `»`), ending with the percent. Local
commands are `:status`, `:again`, `:back N`, `:stop`, `:resume`,
`:compact`, `:quit`.

Then with the microphone:

```sh
cd your-project
cc-voice
```

A rising chime means the mic is up and the speech link answered. Then:

| Say | What happens | You hear |
|---|---|---|
| "operator, ..." | opens a turn; keep talking, pause as long as you like | a soft tick |
| "... over" | sends the turn | a rising two-note run, then "Received." |
| "operator stop" | pause speech | a falling pair (G to E) |
| "operator resume" | resume where it stopped | the same pair rising (E to G) |
| "operator again" / "operator repeat" | replay the last answer | a short high tick, then the answer |
| "operator status" | link, mic, session, what Claude is doing, context fill | the tick, then the status |
| "operator cancel" / "operator never mind" | discard the turn being dictated | the falling "discarded" tone |
| "operator compact" | send `/compact` | the tick, then "Received." |
| "operator quit" | end cleanly (after any turn in flight lands) | the tick, then a falling triad as it exits |

Every command is acknowledged by ear the moment it is heard, so you
never wonder whether it landed. Stop and resume share their two notes
and differ by direction; the quit triad is the startup chime played
backwards.

Anything said without the address word is ignored. "operator" counts
only as the first word you say; "over" counts only as the last word,
followed by a short silence, so "bring that over to the other file"
keeps going. Saying the address word while cc-voice is talking pauses
it; it resumes after your turn is sent or cancelled. You can speak while
Claude works: the message reaches it at the next tool boundary.

The speech-to-text connection exists only while you talk. A small
local voice detector opens it at your first word (the half second
before is kept and sent too, so nothing is lost to the connect) and
closes it ten seconds after your last, unless a turn is still open, in
which case any pause is fine. Idle time costs nothing on your plan and
holds no connection.

Useful flags: `--new` starts a fresh claude session instead of resuming
the pinned one, `--resume SESSION_ID` pins a specific one, `--voice ID`
overrides the voice, and anything after `--` goes to claude
(`cc-voice -- --permission-mode acceptEdits`).

## How it works

One Python process drives one persistent `claude -p` child over its
stream-json interface, with the spoken-block contract passed as a
system-prompt file on every spawn (there is nothing to install on the
Claude side). Claude ends each response with a fenced voice block; a
Translator turns the stream into narration, spoken lines and the percent
closer; a SpokenLog plays a cursor through everything said, which is
what makes pause, resume and replay work. On the way in, a gate with a
local voice detector (WebRTC's) owns the speech-to-text session, so
the TurnMachine, the watchdogs and playback never learn which vendor
is listening; each vendor lives in one adapter behind a small
interface (`cc_voice/providers/`).

Session state lives under `~/.local/state/cc-voice/projects/<project>/`:
the pinned session id, a lock (so two cc-voices never drive one session,
and a stray child from a crashed run can be found and killed on the next
start), a private log of every record heard and spoken, and the contract
file. The child closes after 30 idle minutes and respawns on the next
turn.

## Permissions

In `claude -p` mode nobody is at the keyboard to answer "Allow this?".
Whatever your Claude Code settings would prompt for is, for now,
answered as denied. Pre-approve what you want in your settings, or pass
a permission mode through: `cc-voice -- --permission-mode acceptEdits`.
Answering prompts by voice is the next thing to build.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The suite is offline: no network, no audio device, no `claude`. Vendors
are faked behind the provider interface (one contract suite runs over
every speech-to-text adapter) and the child is a scripted subprocess.

To hear the real vendors without a microphone, `tools/live_check.py`
synthesizes an utterance with Cartesia, pads it with silence, and
pushes it through the real gate and adapter at real-time pace:

```sh
CARTESIA_API_KEY=... .venv/bin/python tools/live_check.py --stt cartesia
DEEPGRAM_API_KEY=... CARTESIA_API_KEY=... .venv/bin/python tools/live_check.py --stt deepgram
```

## License

MIT.
