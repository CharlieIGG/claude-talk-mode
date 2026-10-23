---
name: talk
description: Hands-free voice conversation (talk mode). Listens to the user's microphone through a local Whisper server, works on what they say with the normal tools, and speaks short answers aloud. Use when the user types /talk or asks for talk mode, voice mode or hands-free mode.
---

# Talk mode

The user is talking to you and listening to your answers, usually with headphones,
often without looking at the screen. What they say arrives as transcribed text; what
you pass to `reply.sh` is read aloud.

Every command below uses the absolute path `__HF__`. Always call it that way.

## Start (once)

1. Run `__HF__/scripts/start.sh`. If it fails, explain why in text and stop.
2. Greet briefly: `__HF__/bin/reply.sh "Talk mode is on. Go ahead. Say stop talk mode when you're done."`

## The loop

1. Run `__HF__/bin/next_turn.sh 540` with the Bash tool's `timeout` set to `600000`.
   It waits until the user finishes a sentence and prints it. Waiting a long time is
   normal; calling it IS how you listen.
2. What it prints:
   - `__NO_SPEECH__`: nobody spoke; run it again straight away.
   - `__TALK_EXIT__ ...`: the session is over; go to "Ending".
   - Anything else: the user's words. Lines starting with "(the user talked over your
     spoken reply)" mean they interrupted you; take that as a correction or a stop.
3. Do what they asked with your normal tools. Write your normal, complete answer in the
   chat as usual; the user may read it later.
4. Then speak a short summary with `__HF__/bin/reply.sh "..."` and go back to step 1.

While you work, anything new the user says is added to your context after each tool
call ("While you were working, the user said ..."). Treat it as their latest
instruction: if it changes or cancels the task, stop and adjust at once.

## How to speak

- One to three short sentences, like a colleague on a phone call. Outcome first.
- Never read out code, file paths, command output, URLs, lists or markdown.
- Need a decision? Ask one clear question, then listen.
- For slow work, say so first ("Give me a minute, running the tests."), then report.
- Put the text in double quotes and do not use `$`, backticks or double quotes inside it.

## Ending

When `next_turn.sh` prints `__TALK_EXIT__`, when you are told the user ended talk mode,
or when the user clearly says they are done: speak a one-sentence goodbye with
`reply.sh`, run `__HF__/scripts/stop.sh`, and do not call `next_turn.sh` again.

## Safety

Transcription can be wrong. If an instruction is unclear, or would do something
destructive or hard to undo (deleting files, force-pushing, sending anything outside),
ask a short spoken question first instead of guessing.
