# earshot

A voice interface for Claude Code. You talk to your coding agent, and it
talks back.

Run `earshot` in a project folder. Say the address word ("operator"),
talk, and end with "over". Your words go to your own installed `claude`
as a message. The full answer appears in the terminal, and a short
spoken summary is read aloud, followed by the context-window fill
("32 percent."), which is how you know the turn is over.

Status: the first usable version. Text and voice both work end to end;
voice permission prompts, interrupting a running query, and `earshot
setup` are not built yet. See `docs/design.md` for the design and its
open questions.

## What you need

- macOS (Linux should work; Windows is not supported yet).
- Python 3.11 or newer.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed
  and logged in. earshot never handles the login; it drives the `claude`
  binary on your PATH under your own account.
- Headphones. earshot scrubs its own control words from everything it
  speaks, but on open speakers the mic still hears the voice.
- For voice mode, two API keys:
  - [Deepgram](https://deepgram.com) for speech to text (`DEEPGRAM_API_KEY`).
  - [Cartesia](https://cartesia.ai) for text to speech (`CARTESIA_API_KEY`),
    plus the id of a Cartesia voice (pick one in their playground and
    copy its id).

Text mode needs no keys and no audio device.

## Install

From a checkout:

```sh
git clone git@github.com:Nick-Yawn/earshot.git
cd earshot
python3 -m venv .venv && .venv/bin/pip install -e .
```

or with [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e .`

That puts an `earshot` command in the venv (`.venv/bin/earshot`). The
audio layer uses `sounddevice`, which needs PortAudio; on macOS the
wheel bundles it. On Linux, `apt install libportaudio2` first.

## Configure

Create `~/.config/earshot/config.toml`. Only the voice id is required:

```toml
[tts]
voice = "your-cartesia-voice-id"

[words]
address = "operator"   # the word that opens a turn
closer = "over"        # the word that sends it

[volumes]
speech = 1.0           # what Claude says to you
narration = 0.5        # tool-call narration, quieter by design
earcons = 0.6

[seat]
# extra flags for the claude child, e.g. to pre-approve edits:
# claude_args = ["--permission-mode", "acceptEdits"]

[respell]
# how the voice should say jargon it mangles
# dev = "devv"
```

A project can override any of it with a `.earshot.toml` in its folder.
Keys come from the environment, never the config file:

```sh
export DEEPGRAM_API_KEY=...
export CARTESIA_API_KEY=...
```

earshot removes both keys from the child claude's environment.

## Run

Try it without audio first:

```sh
cd your-project
earshot --text
```

Type a message and press Enter. You'll see claude start, the message go
out, tool calls narrated as they happen, the full answer, and then the
lines that would be spoken (marked `»`), ending with the percent. Local
commands are `:status`, `:again`, `:back N`, `:stop`, `:resume`,
`:compact`, `:quit`.

Then with the microphone:

```sh
cd your-project
earshot
```

A rising chime means the mic and the speech link are up. Then:

| Say | What happens |
|---|---|
| "operator, ..." | opens a turn (a soft tick confirms it); keep talking, pause as long as you like |
| "... over" | sends the turn (a rising two-note run, then "Received.") |
| "operator stop" / "operator resume" | pause / resume speech where it stopped |
| "operator again" / "operator back two" | replay the last line, or N back |
| "operator status" | link, mic, session, what Claude is doing, context fill |
| "operator cancel" | discard the turn being dictated |
| "operator compact" | send `/compact` |
| "operator quit" | end cleanly (after any turn in flight lands) |

Anything said without the address word is ignored. "operator" counts
only as the first word you say; "over" counts only as the last word,
followed by a short silence, so "bring that over to the other file"
keeps going. Saying the address word while earshot is talking pauses
it; it resumes after your turn is sent or cancelled. You can speak while
Claude works: the message reaches it at the next tool boundary.

Useful flags: `--new` starts a fresh claude session instead of resuming
the pinned one, `--resume SESSION_ID` pins a specific one, `--voice ID`
overrides the voice, and anything after `--` goes to claude
(`earshot -- --permission-mode acceptEdits`).

## How it works

One Python process drives one persistent `claude -p` child over its
stream-json interface, with the spoken-block contract passed as a
system-prompt file on every spawn (there is nothing to install on the
Claude side). Claude ends each response with a fenced voice block; a
Translator turns the stream into narration, spoken lines and the percent
closer; a SpokenLog plays a cursor through everything said, which is
what makes pause, resume and replay work.

Session state lives under `~/.local/state/earshot/projects/<project>/`:
the pinned session id, a lock (so two earshots never drive one session,
and a stray child from a crashed run can be found and killed on the next
start), a private log of every record heard and spoken, and the contract
file. The child closes after 30 idle minutes and respawns on the next
turn.

## Permissions

In `claude -p` mode nobody is at the keyboard to answer "Allow this?".
Whatever your Claude Code settings would prompt for is, for now,
answered as denied. Pre-approve what you want in your settings, or pass
a permission mode through: `earshot -- --permission-mode acceptEdits`.
Answering prompts by voice is the next thing to build.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The suite is offline: no network, no audio device, no `claude`. Vendors
are faked behind the provider interface and the child is a scripted
subprocess.

## License

MIT.
