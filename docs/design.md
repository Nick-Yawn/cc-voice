# earshot: design, first draft

A voice interface for Claude Code. You talk to your coding agent, and it talks back.

Status: a proposal for Nick to react to, not a spec. Written 2026-09-22 from the private voice loop in the Cortana repo. Anything marked **learned in use** comes from a real session.

---

## 1. Goals and non-goals

**Goals**

- An ordinary Claude Code user (Nick's buddy first) installs one package, adds two API keys, runs `earshot` in a project folder, and codes by voice.
- It drives the user's own installed `claude`. earshot never handles the Claude login.
- Two output channels. The full answer appears in the terminal, and a short spoken summary is played aloud.
- Speech providers can be swapped. Deepgram (speech to text) and Cartesia (text to speech) are the defaults.
- The Cortana loop's lessons ship as defaults, so nobody has to relearn them.

**Non-goals**

- Nothing from Cortana: no brain server, no bodies, no Fly, no journal, no memory, no second voice.
- It doesn't replace the terminal.
- It isn't a general assistant. It drives one Claude Code session in one folder.
- It never starts work on its own. Every query is the user's words, or background work those words started.

---

## 2. What using it feels like

**Start.** Run `earshot` in a project folder. It asks which session to resume (or starts a new one), plays a rising chime when the mic and link are up, and shows a live transcript pane.

**Open a turn.** Say the address word ("operator" by default), then talk. Anything said without it is ignored. **Learned in use:** in session one, OS dictation typed a stranger's "are you still at church?" into the terminal. A soft tick confirms that the turn opened.

**End a turn.** There are two ways, and both matter:

- **A closer word.** Say "over" at the end and the turn is sent right away. **Learned in use:** Nick pauses for a long time mid-thought. Every silence-based or meaning-based end-of-turn detector either cut him off or was tuned so loose it became a closer word with extra steps. For people who think out loud, the closer is a feature.
- **Inferred end-of-turn.** The speech provider decides you're done (Deepgram Flux and Cartesia Ink 2 do this natively). This is for people who won't say "over". "over" still closes a turn instantly in this mode.

The closer counts only at the **end** of what you say: the word, then about 400 ms of silence. **Learned in use:** "I'm gonna bring that over to…" once closed a turn mid-sentence.

**Confirmation.** A dispatch tick, then "Received." The rule is **earcons for machinery, voice for content.** **Learned in use:** with no capture signal, Nick said the same thing three times and got three answers.

**While it works.** Tool calls are narrated in a quieter, quicker voice ("Running the test suite."). Anything said *to* you plays at full volume. **Learned in use:** half volume at 1.2x reads as "talking to yourself". Long turns open with a one-line spoken plan and add a line at each milestone. Those lines are what you interrupt against. After 15 quiet seconds, a soft still-here tone plays.

**The answer.** Two to four spoken sentences, with anything that needs your decision first. Then the closer, which is the context-window fill ("32 percent."). **Learned in use:** the percent became Nick's preferred closer. It tells you the turn is over and when to compact.

**Talking over it.** Saying the address word pauses playback. Playback resumes from that point after your turn is sent.

**Stacking.** Speak while Claude works, and your message reaches it at the next tool boundary, just like typing into the interactive CLI.

**Vocal commands.** Say "operator" plus a command. These run locally and cost no Claude turn.

| Say | Does |
|---|---|
| stop / resume | pause / resume speech where it stopped |
| again, back two | replay the last line, or N back |
| status | link, mic, session, what Claude is doing, context fill |
| cancel | discard the turn being dictated |
| interrupt | stop Claude's running query (probe first, §9) |
| compact | send `/compact` |
| allow / deny | answer a permission question (§7) |
| quit | end cleanly |

---

## 3. Architecture

**Recommendation: one Python host process that drives one persistent `claude -p` child over stream-json.**

```
   mic ──► [STT provider] ──► TurnMachine  (address, closer, commands)
                                   │
   ┌────────────── earshot host (asyncio) ──────────────────┐
   │  Seat: claude -p --input-format stream-json            │
   │        --output-format stream-json --verbose           │
   │        --replay-user-messages --resume <session>       │
   │        --append-system-prompt-file voice-contract.md   │
   │  stdin  ◄── your messages, permission answers          │
   │  stdout ──► Translator ──► SpokenLog                   │
   │             (tools, ⟦voice⟧ blocks, result, percent)   │
   └────────────────────────────────────┬───────────────────┘
                                        ▼
   terminal pane (full text)    SpokenLog cursor ──► [TTS provider] ──► speaker
```

- **One process per session.** It is spawned on the first turn and resumed from a session id pinned on disk. It closes after 30 idle minutes and respawns on the next turn. Each message goes to stdin the moment it's heard.
- **An ordered channel.** Words go in and output comes out, in order, with no turn tracking. **Learned in use:** Cortana first matched each output record to the turn that caused it, and every hard bug of the first live nights lived in that matching.
- **The Translator** is a pure function over stream records. It computes the context percent from the *last* usage iteration. **Learned in use:** the top-level usage adds up every round and once reported "108 percent". It skips subagent records (those with a `parent_tool_use_id`). **Learned in use:** without that, about 50 "Running a command." lines were spoken in a row.
- **The SpokenLog** replaces the usual speech queue. Every line earshot says is appended to a log, and playback is a cursor through it. Pause, resume, replay-N and pause-while-talking all fall out of that.

**Alternatives considered**

- **The Agent SDK.** It spawns the same CLI over the same protocol and wraps interrupt and permission callbacks in typed APIs. It's a fair choice, and a thin `Seat` interface keeps it swappable. Start raw anyway, for four reasons:
  - The raw path is proven daily.
  - It needs no extra dependency.
  - It sees every record, including task notifications and `origin`.
  - It loads the user's settings, CLAUDE.md, skills and MCP exactly as their normal session does.

  The SDK's docs also steer third-party products toward API keys (open question 1).
- **Hooks on the interactive TUI.** A Stop hook could speak each answer while the user keeps their normal terminal. That covers output only. Input is the hard half, and typing into a terminal is what failed in session one. It's a possible later "voice out, keyboard in" mode.
- **One `claude -p` call per turn.** This was the original loop, and it's retired. Messages you stacked while Claude worked had to wait for the query to finish, then burst out together.

**Compliance posture** (carried over from Cortana). earshot uses the official CLI through its documented headless interface, under the user's own login. It never reads credentials, scrapes a session or runs unattended. The idle close keeps a headless process from sitting open on the subscription.

---

## 4. The spoken-block contract

Every response ends with:

```
⟦voice⟧
One decision needs you: keep the retry or drop it. Everything else
landed: four files, tests green.
⟦/voice⟧
```

The rules, all learned in use:

- **Full fidelity first.** The block summarizes the answer and never replaces it.
- **Two to four spoken sentences, decisions first.** No paths, code, hashes or symbols. Name things by what they are.
- **Blocks stand alone.** Never say "see your screen", because some users only listen.
- **Long turns get a plan block before the first tool call and a progress block at each milestone.** Each mid-turn block must follow a plain sentence in the same breath. A block written straight after hidden reasoning ends up in the thinking channel and is never spoken; we measured this.
- **Long work runs in the background, and the query then ends.** This covers subagents, CI waits and builds. A foreground wait holds the user's stacked messages until it returns. **Learned in use:** three foreground reviewers sat on Nick's "you still with me?" for 9 minutes. If a stacked message lands at a boundary, answer it right there.
- **Never speak the address word or a control phrase.** earshot also scrubs them from speech.
- **If dictation is ambiguous, ask one short question.**

**Recommendation: pass the contract as a system-prompt file (`--append-system-prompt-file`) on every spawn.**

- There's no install step.
- It survives compaction. Cortana re-invokes a skill on every process start to get the same guarantee.
- It touches only earshot sessions, so the user's terminal sessions are unchanged.

A skill or an output style would both depend on Claude loading them, and an output style replaces more of the default prompt than we want. If earshot later grows commands or agents, it can ship them as a plugin via `--plugin-dir`, which also needs no install.

---

## 5. The provider interface

The core never calls a vendor directly. Each adapter declares what it can do, and the core fills in the gaps.

**Speech to text**

```python
class STT(Protocol):
    caps: STTCaps   # partials, word_timings, native_turns, keyterms, streaming
    async def open(self, rate=16000, keyterms: list[str] = (),
                   language="en") -> STTSession: ...

class STTSession(Protocol):
    async def send(self, pcm16: bytes) -> None: ...
    def events(self) -> AsyncIterator[STTEvent]: ...
    async def close(self) -> None: ...

STTEvent = Partial(text) | Final(text, words: list[Word] | None)
         | SpeechStarted() | TurnEnd(text, eager: bool) | TurnResumed() | Error(msg)
Word = (text, start_s, end_s, confidence)
```

- **Finals** are the minimum an adapter must provide. They feed the TurnMachine.
- **Partials** drive the live pane and let pause-while-talking react early.
- **Word timings** make the closer rule exact: "over" counts only if silence follows the word's end. Without them, the core falls back to a timer.
- **Native turn events** power inferred mode. Without them, the core runs its own silence-based endpointer.
- **Keyterms** boost the address and closer words. **Learned in use:** the address word became reliable only once it was boosted. `earshot setup` warns when an adapter can't boost.
- **Non-streaming engines** (local Whisper, upload APIs) get a local VAD that cuts speech into segments and emits Finals.

**Text to speech**

```python
class TTS(Protocol):
    caps: TTSCaps     # streaming, speed, pauses, word_timestamps, pronunciation
    sample_rate: int
    def speak(self, text: str, *, voice: str, speed=1.0
              ) -> AsyncIterator[bytes]: ...   # PCM16 mono; cancel = stop iterating
```

- **The core handles sentence splitting and volume.** The adapter only turns text into audio.
- **Speed is explicit.** **Learned in use:** Cartesia honors speed only inside `generation_config` and silently ignores a top-level field.
- **Word timestamps are optional.** Where they exist, resume can pick up at the exact word.

| | Speech to text | Text to speech |
|---|---|---|
| Default | Deepgram Nova-3 streaming (Flux for inferred turns) | Cartesia Sonic 3.6 |
| Cloud | Cartesia Ink 2, OpenAI realtime transcription, ElevenLabs Scribe | OpenAI TTS, ElevenLabs |
| Local, no key | faster-whisper / whisper.cpp + VAD | macOS `say`, Piper, Kokoro |

---

## 6. Packaging, install, config

- **Python 3.11+ on PyPI, run with `uvx` or `pipx`.** The proven code is Python, and `sounddevice` (PortAudio) handles audio. The name `earshot` is taken on PyPI (§11).
- **No Claude Code plugin in v1.** earshot spawns Claude and hands it the contract, so there's nothing to install on the Claude side.
- **`earshot setup`.** It picks the mic and output device, checks each key with a live request, plays the voice, and runs a 30-second hearing test of the address and closer words.
- **Config** lives in `~/.config/earshot/config.toml`: providers, voice, words, mode, volumes, respell map. Projects can override it with `.earshot.toml`.
- **Keys** come from environment variables or the OS keychain, never the config file. **earshot removes its own keys from the child Claude's environment.** The agent runs shell commands and has no need to see them.
- **A zero-key path on macOS** (local Whisper plus `say`) lets people try earshot before signing up for anything.

**Platforms.** macOS first, since that's where it has been lived in. Linux second, with Piper standing in for `say`. Windows and WSL are left out of v1 because of audio device pain.

---

## 7. Permissions and safety

**The problem.** In `-p` mode nobody is at a keyboard to answer "Allow this command?". Cortana's seat runs in `auto` permission mode. That's fine for its owner, but it isn't a default for a stranger.

**The proposal.** Keep the user's own permission mode, and send prompts to the host (`--permission-prompts host`). earshot receives them on the stream-json control channel; the exact message shape needs a probe, alongside interrupt.

1. **It reads the request back in plain words:** "Claude wants to delete the build folder. Allow?"
2. **Only an address-framed "operator allow" or "operator deny" counts.**
3. **Silence or anything unclear means deny.** After 60 seconds the request is refused, and Claude is told why.
4. **The terminal always shows the raw request.** `y` or `n` on the keyboard also answers.
5. **`bypassPermissions` is never a default.** `--permission-prompts none` (auto-deny) is offered for people who prefer to pre-approve in settings.

**Other hazards**

- **Other voices.** People, a TV or a call could say the address word. Framing lowers that risk but doesn't remove it, and the README says so. Speaker verification is out of v1.
- **Two drivers on one session.** earshot locks the session id and refuses to resume a session another driver holds. **Learned in use:** two processes on one Cortana session both acted as the lead.

---

## 8. Out of v1

- Wake words and always-on listening beyond the address word.
- Speaker verification.
- Open-speaker echo cancellation (headphones are recommended; macOS voice-processing I/O is the obvious v1.x).
- Windows.
- More than one session at a time.
- Agent-initiated "knocks".
- Reading tone of voice.
- Custom or cloned voices.
- A GUI.
- Canned filler. **Learned in use:** Cortana retired it, because a wrong-guess filler breaks presence worse than silence.

---

## 9. Known problems we designed for

Each of these is real in the private loop: still open there, or fixed late at some cost.

1. **Orphaned Claude processes.** When Cortana's seat died, its `claude` child kept running under launchd as a second lead, twice. The fix was ruled but never built.
   → *Day one:* the child runs in its own process group. Exit, signals and `atexit` all close stdin, then send SIGTERM and SIGKILL to the whole group. The lock file records the child pid, so on start earshot finds and offers to kill any stray process.
2. **The idle-boundary fix set that was never built.** A turn cap refused messages. A `--resume` replayed a stale notification, and "No voice summary" was spoken. Foreground waits swallowed stacked messages. No parsed result log existed.
   → *Day one:* there's no cap. An empty result from a machine-started query (one whose result carries an `origin` field) is logged, never spoken. The background-and-end rule is in the contract. A private local log records every raw record and every heard, dropped, sent and spoken event.
3. **Replayable speech.** Nick found "droppable narration" to be the wrong trade and wants pause, resume and "replay N".
   → *Day one:* the SpokenLog. Nothing is dropped; the cursor just moves past it.
4. **Pause while I talk.** The speaker talked over Nick after he opened a turn.
   → *Day one:* the address word pauses the cursor, and sending or cancelling the turn resumes it. New answers queue behind the paused one.
5. **Volume and mic mute by voice.**
   → *Partly.* "louder" and "quieter" are cheap and can ride along. Mute by voice is a trap, because a muted mic can't hear "unmute", so it gets a hotkey.
6. **Words the TTS mispronounces.** "dev" was spelled out letter by letter. Heteronyms were misread: "record" the verb as the noun, and "closer" with a soft s.
   → *Day one:* a user-editable respell map for jargon, applied to the audio only. Heteronyms are left to the provider, because a blanket respell breaks the other reading (Sonic 3.6 claims to handle them).
7. **Keyword or mention.** Is "operator" or "over" a command, or just said in passing?
   → *Day one:* position rules. The address word counts only as an utterance's first word. The closer counts only as its final word followed by silence. A command is the address word plus the command, with nothing after it, except "stop", which fires anywhere because a false stop only pauses speech. Tolerating filler before the address word ("um, operator") is a knob, off by default. The "break break" hold-the-turn word is out of v1, because closer mode already tolerates pauses.
8. **People who pause while thinking.** Inferred end-of-turn cuts them off.
   → *Day one:* the closer word is the default mode, and it works in inferred mode too.
9. **Self-hearing.** An open mic heard about 30 of the loop's own lines in one session.
   → *Day one:* all speech is scrubbed. The address word is clipped to "op" and a bare "over" to "ov", so earshot's own audio can never open, close or command anything. Headphones are recommended. On speakers, a transcript that matches what was just played is dropped as echo. Real echo cancellation is v1.x.
10. **Going deaf after playback.** Audio kept flowing, but no transcripts came back and no error was raised.
    → *Day one:* two watchdogs. One rebuilds the mic stream when frames stop. The other restarts the STT session when speech energy arrives with no transcript.
11. **Interrupting a running query.** This was never built, because the control message looked undocumented.
    → *Probe first.* The Agent SDK's `interrupt()` uses that same channel. A version-pinned probe test decides whether it goes into v1.
12. **Re-addressing your own open turn.** It appended, which Nick called odd.
    → *Day one:* the address word opening a new utterance, after more than 2 seconds of quiet in an open turn, discards that turn and starts fresh. A falling tone marks the discard.
13. **Compaction is slow.** It took 137 seconds live.
    → *Day one:* "Compacting." and "Compacted." marks. Messages spoken during the window are queued by the CLI (measured), not lost.

---

## 10. Build order (sketch)

1. The Seat driver and Translator with a text front end. Port Cortana's pure parsers and their tests.
2. The TurnMachine, the Deepgram and Cartesia adapters, the SpokenLog, earcons and scrubbing.
3. Voice permissions, the interrupt probe, `earshot setup`.
4. The local, OpenAI and ElevenLabs adapters.
5. A 10-minute counted live trial with the buddy before tuning anything (Cortana's perceptual-loop rule).

---

## 11. Naming

A name containing "Claude" likely clashes with Anthropic's brand guidelines. "for Claude Code" is fine as a description.

- **earshot.** It says what it does. It's taken on PyPI, so it would ship as `earshot-voice`, which is free.
- **overandout.** The radio protocol the tool actually uses. Free on PyPI.
- **rogerwilco.** "Heard, will do", which is exactly the loop. Free on PyPI.
- **radiocheck.** "Can you hear me?". Free on PyPI.

Avoid "Operator" as the product name, because it's an OpenAI product. It's fine as the address word.

---

## 12. Open questions for Nick

1. **Terms posture.** Is it OK for a public tool to drive a user's own `claude` under a Pro/Max subscription? *Recommend:* design it as drawn here (never touch auth, one human's cadence, idle close, nothing unattended), say so in the README, and check Anthropic's current CLI and Agent SDK terms before the public release.
2. **Default turn ending for the buddy.** *Recommend:* the closer by default and inferred as opt-in, then let his 10-minute counted trial decide.
3. **Permissions by voice, or keyboard only?** *Recommend:* voice with read-back and deny-on-silence, the keyboard always available, never bypass by default.
4. **Default turn opening.** An address word over an open cloud mic, or a push-to-talk hotkey? Continuous Deepgram streaming costs about $0.46 an hour. *Recommend:* the address word with a local VAD gate so silence isn't streamed, and a hotkey as an option.
5. **Default address word.** *Recommend:* "operator", configurable.
6. **Raw CLI or Agent SDK?** *Recommend:* raw now, behind a `Seat` interface.
7. **Code lineage.** *Recommend:* copy the pure pieces (TurnMachine, parser, narration, scrub) and their tests into earshot, and later have Cortana's seat depend on earshot, not the reverse.
8. **License.** *Recommend:* MIT.
9. **The percent as every turn's closer, even for strangers?** *Recommend:* yes by default, with a setting to switch to a tone.
10. **Name.** *Recommend:* "earshot", shipped as `earshot-voice`.
