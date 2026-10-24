# Talk mode for Claude Code

> **macOS only** (Apple Silicon). It relies on macOS's `say`, `afplay`, `osascript`,
> AVFoundation audio capture, the unified system log and launchd. Linux and Windows are
> not supported. See [Known limits](#known-limits).

Hands-free voice conversations with [Claude Code](https://code.claude.com) in the
terminal. Type `/talk`, then just speak: pause, and Claude answers out loud, then listens
again. Talk over it to interrupt; speak while it works to change course; say
"stop talk mode" to end.

Everything runs on your Mac. Speech recognition is local
[whisper.cpp](https://github.com/ggml-org/whisper.cpp), the voice is macOS's built-in
`say`, and the glue is about 1,000 lines of shell and standard-library Python that you
can read in one sitting. No API keys, no accounts, no cost.

Optional extra: a **double tap on your Bluetooth earbuds mutes the Mac's microphone for
every app** (Teams, Zoom, talk mode) while a microphone is in use.

## Why this project exists

We wanted a phone-app-style voice conversation with Claude Code on a work laptop, with
no new licences and nothing we couldn't review. When we evaluated the options
(October 2026), none fit:

| Option | What it is | Why it didn't fit |
|---|---|---|
| Claude Code's built-in `/voice` | Push-to-talk dictation into the prompt | Not hands-free: you hold or tap a key every turn, and answers are not spoken. |
| [VoiceMode](https://github.com/mbailey/voicemode) | Full voice plugin + MCP server, the most complete option | About 47,000 lines that change daily, much of it AI-written with little review. Installs speech services that start at login, with the Whisper server listening on all network interfaces. Writes conversation transcripts to disk with no off switch. Falls back to OpenAI's paid API when the local engines fail, if an OpenAI key happens to be set. |
| [mcp-voice-hooks](https://github.com/johnmatthewtennant/mcp-voice-hooks) | Hooks + browser speech recognition | No licence, so no right to use it. Speech recognition runs in the browser (in Chrome, on Google's servers). |
| [handsfree-claude](https://github.com/Nimeshka/handsfree-claude) | Small, local, hands-free (MIT) | The right design, and this project's starting point. But it depends on a prebuilt speech binary from a young project, and had no way to interrupt Claude, no mid-task instructions, and no guarantee the microphone closes. |

So this is a rewrite of handsfree-claude's design with these goals:

- **Small and readable.** About 1,000 lines, standard-library Python on macOS's own
  `/usr/bin/python3`, no `pip install`.
- **Local only.** Whisper listens on `127.0.0.1` only. Audio is never saved; transcripts
  exist only during a session.
- **Nothing running when you're not talking.** Everything starts with `/talk` and stops
  with it (the optional mute watcher is the one exception).
- **Behaves like a conversation:** interrupt by talking, give instructions mid-task,
  audible cues so you know you were heard.

## Requirements

- macOS on Apple Silicon (tested on macOS 26.5 with Claude Code 2.1.309)
- [Homebrew](https://brew.sh): `brew install whisper-cpp ffmpeg jq`
- Claude Code CLI
- Headphones or earbuds (so the microphone doesn't hear Claude's voice)
- Your terminal app needs microphone access (macOS asks the first time)

## Install

```sh
git clone https://github.com/CharlieIGG/claude-talk-mode.git   # any location works
cd claude-talk-mode
./install.sh
```

The folder can live anywhere: `install.sh` writes its own absolute path into the skill
and settings, so re-run it if you move the folder.

`install.sh`:

1. Downloads the Whisper model `ggml-small.en.bin` (466 MB) from Hugging Face and
   checks its SHA-256 against the published one.
2. Installs the `/talk` skill into `~/.claude/skills/talk`.
3. Adds one hook (on four events) and four allowed commands to
   `~/.claude/settings.json`, after backing it up. It only adds entries.

`./uninstall.sh` removes all of that again. Start a new Claude Code session afterwards.

## Use

- **Start:** type `/talk`. The first line says which microphone is in use.
- **Talk:** just speak. A pause of about 1.2 seconds ends your turn.
- **Sounds:** a "Glass" chime when your turn ends means "heard you". A quiet "Pop" every
  4 seconds means Claude is still working on it.
- **Spoken status:** if Claude has worked for a minute without saying anything, you hear
  "Still working. Right now: ..." with what it is doing at that moment (taken from its
  current command), repeated every minute. Useful when the pops are drowned out.
- **Interrupt:** start talking while Claude speaks. Its voice stops within about a third
  of a second, and replies it had queued are dropped. Headphones only: on speakers the
  microphone ignores everything while Claude speaks, so it doesn't hear itself.
- **Change course mid-task:** just say it. Claude receives it after its current step,
  usually within seconds.
- **Make Claude stop working:** say "hold on" or "hang on" on its own while Claude is
  busy. You hear a "Submarine" sound, and from then on every tool call Claude tries is
  refused (its subagents' too) until it has asked you what you want. A command that is
  already running still finishes first, and so does a stretch where Claude only writes.
- **Stop:** say "stop talk mode" (the microphone closes immediately), press Esc and then
  type anything, or stay silent for 10 minutes.
- **Permission prompts:** if Claude Code asks for approval during talk mode, you hear a
  "Hero" sound and "I need your approval on the screen".
- **Status / emergency stop** from any terminal: `bin/talkctl.py status`,
  `scripts/stop.sh`.

## How it works

```
microphone ──ffmpeg──▶ listen.py ──(pause = end of turn)──▶ whisper-server (127.0.0.1)
                          │                                        │
                          │ talking over Claude stops `say`        ▼
                          │                               runtime/inbox.jsonl
                          ▼                                        │
                       speak.py ◀── runtime/outbox.jsonl ◀── reply.sh      next_turn.sh ──▶ Claude
                       (`say`)                                 ▲                  │
                                                               └──── Claude ◀─────┘
```

- Claude listens by calling `bin/next_turn.sh`, which blocks until you finish a sentence.
  It answers by calling `bin/reply.sh`. The `/talk` skill tells it to loop.
- `hooks/talk-hook.sh` connects talk mode to Claude Code. It only acts in the session
  that started talk mode, and does nothing otherwise (about 12 ms per call):
  - **PostToolUse:** passes on what you said while Claude was working.
  - **Stop:** keeps Claude in the listening loop instead of ending its turn.
  - **UserPromptSubmit:** typing a message ends talk mode and closes the microphone.
  - **PermissionRequest:** makes approval prompts audible. It never answers them.

## Optional: earbud double tap = mute

```sh
scripts/tapmute-install.sh            # start now and at every login
scripts/tapmute-install.sh --remove   # undo
```

While a microphone is in use, Bluetooth earbuds send the headset "voice recognition"
command on a double tap. `bin/tapmute.py` watches for it in the system log and switches
the Mac's microphone level between 0 (complete silence) and its previous level, saying
"Muted" or "Microphone on". This works in any app, including Teams calls. Without an
active microphone, a double tap stays "next track".

- **Switch Siri off on the Mac**, otherwise Siri opens on every double tap.
- It uses about 5 MB of memory and no measurable CPU; it sleeps until a double tap
  arrives.
- If you mute and then end a call, the level stays at 0 until you double tap during the
  next call, or log in again.
- Tested with Google Pixel Buds Pro 2. Other earbuds that send the standard headset
  command should work too.

## Settings (`config.json`)

| Setting | Meaning |
|---|---|
| `audio.input_device` | `default` = the microphone macOS currently uses (your earbuds while you wear them, so you can walk around), or a fixed device name from `ffmpeg -f avfoundation -list_devices true -i ""`. A Bluetooth earbud microphone switches the earbuds to call-quality audio; the built-in microphone keeps full quality but only hears you near the laptop. |
| `vad.silence_ms` | Pause that ends your turn. |
| `vad.min_threshold`, `vad.noise_multiplier` | How loud counts as speech. The room is measured at start; raise these if background noise triggers it. |
| `vad.barge_in_ms` | How long you must talk before Claude's voice stops (so a cough doesn't). |
| `barge_in` | `"auto"` (default) checks the macOS output every 5 seconds: on headphones (Bluetooth, or a device named headphones/headset) talking over Claude stops its voice; on speakers that is switched off and the microphone ignores Claude's own voice and sounds, so it cannot hear itself. `true` / `false` force one behaviour. |
| `stt.model`, `stt.language`, `stt.port` | Whisper model, language and local port. |
| `tts.voice`, `tts.rate` | Voice and speed in words per minute. More voices: System Settings → Accessibility → Spoken Content. If the voice is missing, the system voice is used. |
| `cues.*` | Sound files and volumes for "heard you", "still working", "approval needed" and "interrupt heard" (`""` switches one off). |
| `session.idle_minutes` | Silence after which talk mode ends. |
| `session.status_every_s` | Seconds of Claude working silently before a spoken "still working" update, and between updates (`0` switches them off). |
| `stop_phrases` | Phrases that end talk mode instantly, when said on their own (up to three extra words such as "okay" or "please"); inside a longer sentence they are ignored. |
| `interrupt_phrases` | Phrases that stop Claude's work, under the same "said on their own" rule. Pick words you rarely use otherwise ("wait" is too common). |

## Privacy and security

- Audio never leaves your Mac and is never saved. Each utterance exists as a temporary
  file only while it is transcribed.
- What you said is kept in `runtime/inbox.jsonl` during a session and deleted on stop.
  Logs record timings, lengths and levels, never your words.
- The text Claude receives becomes part of your normal Claude Code conversation, like
  typing.
- Whisper only accepts connections from your own Mac.
- Anyone talking loudly nearby can be transcribed as an instruction. The skill tells
  Claude to ask before anything destructive.

## Troubleshooting

- **It doesn't react to your voice:** `logs/listen.log` shows each turn's start and end
  and "loudest sound X (speech counts from Y)". If X stays below Y, the microphone isn't
  hearing you; check `audio.input_device` and your distance.
- **Claude seems to stop listening:** look at the screen. Claude Code may be showing a
  permission prompt (you should hear the "approval" alert).
- **Short sounds or last words missing on Bluetooth:** already handled with trailing
  silence. If it still happens, your earbuds may lag more.
- **Restart:** always use `scripts/start.sh` (or `/talk`). Don't restart `listen.py`
  alone: the speaker notices the listener is gone and shuts talk mode down.
- **Microphone silent in every app:** the mute may still be on. Check with
  `osascript -e 'input volume of (get volume settings)'`; 0 means muted.

## Known limits

- **macOS only**, tested on Apple Silicon with macOS 26.5 and Claude Code 2.1.309.
  Intel Macs may work but are untested.
- **Built for one person's setup and tested there.** Expect to tune `config.json` for
  your microphone and room.
- **The earbud mute** needs Siri switched off on the Mac and has only been tested with
  Google Pixel Buds Pro 2. It depends on a macOS log message, which a future macOS
  update could change.
- **Claude Code can still ask for permission** for talk mode's own commands, even with
  the allow rules in place (seen once in a long session). You then hear the approval
  alert and have to approve on screen.
- English only for now. Other languages need a multilingual Whisper model and
  `"language": "auto"`.
- No echo cancellation for laptop speakers yet: use headphones, or `barge_in: false`.
- Speech is detected by loudness, so noisy rooms may need a higher threshold.
- Every 9 minutes of silence costs one short Claude turn while it keeps listening.
- A single step longer than 10 minutes ends talk mode; raise `session.idle_minutes` if
  you run such steps.

## Credits and licence

MIT licence. The design (inbox and outbox files, a blocking "next turn" command, a skill
that loops) comes from [handsfree-claude](https://github.com/Nimeshka/handsfree-claude)
by Nimeshka Srimal (MIT).
