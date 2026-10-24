#!/usr/bin/env python3
"""Shared state and commands for talk mode.

Subcommands:
  wait [SECONDS]   block until the user finishes an utterance, print it, exit
  reply TEXT       queue TEXT to be spoken aloud
  hook             Claude Code hook entry point (reads the hook JSON on stdin)
  key mute|quiet   keyboard keys (talkkeys): pause/resume listening, stop the voice
  claim            before start: fail if another live Claude Code session has talk
                   mode on; else print this session's Claude Code pid
  stop [--quick]   end talk mode: close the mic, stop speaker and Whisper server
  status           print what is running

Standard library only; runs on macOS's /usr/bin/python3 (3.9).
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT = ROOT / "runtime"

INBOX = RT / "inbox.jsonl"          # what the user said, one JSON record per utterance
CURSOR = RT / "inbox.cursor"        # number of inbox lines already handed to Claude
CURSOR_LOCK = RT / "inbox.lock"
OUTBOX = RT / "outbox.jsonl"        # what Claude wants spoken
ACTIVE = RT / "active"              # exists while talk mode is on
OWNER = RT / "owner_session"        # Claude Code session id that started talk mode
OWNER_PID = RT / "owner_pid"        # its Claude Code process; talk mode ends when it dies
HEARTBEAT = RT / "heartbeat"        # touched whenever the owning session is active
LAST_HEARD = RT / "last_heard"      # touched whenever the user says something
STOP_FLAG = RT / "stop"             # contains the reason the session should end
ON_SPEAKERS = RT / "on_speakers"    # exists while macOS plays through speakers (barge_in "auto")
CUE_AT = RT / "cue_at"              # touched whenever a feedback sound starts
SPEAKING = RT / "speaking.lock"     # contains the pid of the running `say`
BARGED = RT / "barged"              # the user talked over the speaker
WORKING = RT / "working"            # Claude is handling a turn (not listening)
LISTEN_PID = RT / "listen.pid"
SPEAK_PID = RT / "speak.pid"
PAUSED = RT / "paused"              # the user paused listening with the microphone key
TERMINAL_TTY = RT / "terminal_tty"  # terminal of the session that owns talk mode
TITLE = RT / "title"                # its tab title without the microphone
KEYS_PID = RT / "keys.pid"          # the talkkeys keyboard watcher
SERVER_PID = RT / "server.pid"      # only present if start.sh started the server
STEP = RT / "step"                  # what Claude is doing right now, for spoken status updates
INTERRUPT = RT / "interrupt"        # what the user said to interrupt Claude; cleared once Claude listens again

EXIT_MARK = "__TALK_EXIT__"
NO_SPEECH = "__NO_SPEECH__"
SCRIPT_MARK = ROOT.name + "/"        # the checkout's folder name, whatever it is


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def touch(path):
    path.touch(exist_ok=True)
    os.utime(path, None)


def read_pid(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill(pid, sig=signal.SIGTERM):
    if pid == os.getpid():                  # speak.py runs cmd_stop itself; finish it first
        return
    if alive(pid):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def listener_alive():
    return alive(read_pid(LISTEN_PID))


def age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


def request_stop(reason):
    STOP_FLAG.write_text(reason)


def play_cue(name, cfg=None):
    """Play a short feedback sound without waiting for it (HF_SILENT=1 only logs)."""
    cues = (cfg or load_config()).get("cues", {})
    path = cues.get(name)
    if not path:
        return
    if os.environ.get("HF_SILENT"):
        log(f"[cue] {name}")
        return
    touch(CUE_AT)
    subprocess.Popen(["afplay", "-v", str(cues.get(name + "_volume", 0.5)), padded(path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def padded(path):
    """A copy of a sound with a second of silence after it. Bluetooth headphones lag
    behind, and afplay closes the audio stream when the file ends, so a short sound
    would otherwise be cut off before it is heard at all."""
    out = RT / "cues" / (Path(path).stem + ".padded.aiff")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-i", path, "-af", "apad=pad_dur=1", str(out)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(out) if out.exists() else path


# --- inbox ------------------------------------------------------------------

def take():
    """Return utterances not yet handed to Claude and mark them as handed over."""
    RT.mkdir(parents=True, exist_ok=True)
    with open(CURSOR_LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            seen = int(CURSOR.read_text().strip())
        except (OSError, ValueError):
            seen = 0
        try:
            lines = INBOX.read_text().splitlines()
        except OSError:
            lines = []
        records = []
        for line in lines[seen:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("text") or "").strip():
                records.append(rec)
        if len(lines) != seen:
            CURSOR.write_text(str(len(lines)))
        return records


def format_records(records):
    out = []
    for rec in records:
        text = rec["text"].strip()
        if rec.get("interrupted"):
            text = "(the user talked over your spoken reply) " + text
        out.append(text)
    return "\n".join(out)


# --- commands ---------------------------------------------------------------

def cmd_wait(timeout):
    idle_s = load_config()["session"]["idle_minutes"] * 60
    start = time.time()
    WORKING.unlink(missing_ok=True)         # listening now, so no "working" ticks
    INTERRUPT.unlink(missing_ok=True)       # an interrupt is over once Claude listens
    STEP.unlink(missing_ok=True)
    while True:
        touch(HEARTBEAT)
        if STOP_FLAG.exists():
            print(f"{EXIT_MARK} {STOP_FLAG.read_text().strip() or 'stop'}")
            return 0
        records = take()
        if records:
            print(format_records(records))
            touch(WORKING)                  # Claude handles this turn from here
            return 0
        if not ACTIVE.exists() or not listener_alive():
            print(f"{EXIT_MARK} not-running")
            return 0
        if age(LAST_HEARD) > idle_s:
            request_stop("idle")
            print(f"{EXIT_MARK} idle")
            return 0
        if time.time() - start >= timeout:
            print(NO_SPEECH)
            return 0
        time.sleep(0.2)


def cmd_reply(text):
    text = text.strip()
    if not text:
        log("usage: reply.sh TEXT")
        return 1
    RT.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX, "a") as f:
        f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")
    touch(HEARTBEAT)
    touch(LAST_HEARD)        # idle means nobody spoke, so a long task doesn't count
    return 0


def step_label(tool, tool_input):
    """A few spoken words for what a tool call does, or None if not worth saying."""
    if not isinstance(tool_input, dict):
        return None
    name = Path(tool_input.get("file_path") or tool_input.get("notebook_path") or "").name
    if tool in ("Bash", "Agent", "Task"):
        return tool_input.get("description") or None
    if tool == "Read" and name:
        return f"reading {name}"
    if tool in ("Edit", "Write", "NotebookEdit") and name:
        return f"editing {name}"
    if tool in ("Grep", "Glob"):
        return "searching files"
    if tool in ("WebSearch", "WebFetch"):
        return "searching the web"
    if tool.startswith("mcp__"):
        server = tool.split("__")[1]                 # claude_ai_Microsoft_365, plugin_<plugin>_atlassian
        if server.startswith("claude_ai_"):
            server = server[len("claude_ai_"):]
        elif server.startswith("plugin_"):
            server = server.split("_", 2)[-1]
        server = server.removesuffix("-mcp")
        return f"working in {server.replace('_', ' ').replace('-', ' ')}"
    return None


def interrupt_note(bin_dir):
    said = INTERRUPT.read_text().strip()
    return (f"The user interrupted you by voice, saying \"{said}\". Stop the task you "
            "were on now and do not continue it. Say in one short sentence with "
            f"{bin_dir}/reply.sh that you stopped and ask what they want, then run "
            f"{bin_dir}/next_turn.sh 540 with the Bash timeout set to 600000. Every "
            "other tool call is refused until you do.")


# --- tab title ----------------------------------------------------------------
# While talk mode is on, its tab title gets a microphone in front: "🎙 <title>".
# Terminals don't let programs read the title, but Claude Code builds it from the
# session's title, which it saves in the transcript (a /rename wins over the
# generated one). Claude Code rewrites the title when it switches between working
# and idle, more often than any hook runs, so the hooks only record the title and
# speak.py re-applies it every second while talk mode is on.

MIC = "\U0001F399"


def session_title(transcript_path):
    try:
        data = Path(transcript_path).read_bytes()
    except (OSError, TypeError):
        return ""
    for kind, field in ((b'"type":"custom-title"', "customTitle"), (b'"type":"ai-title"', "aiTitle")):
        i = data.rfind(kind)
        if i < 0:
            continue
        start = data.rfind(b"\n", 0, i) + 1
        end = data.find(b"\n", i)
        try:
            title = json.loads(data[start:end if end >= 0 else None]).get(field) or ""
        except ValueError:
            continue
        if title:
            return title
    return ""


def ps_fields(pid):
    """(parent pid, tty, command name) of a process, or None."""
    out = subprocess.run(["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.split(None, 2)
    return (int(out[0]), out[1], out[2].strip()) if len(out) == 3 else None


def is_claude(pid):
    fields = pid and ps_fields(pid)
    return bool(fields) and os.path.basename(fields[2]) == "claude"


def claude_pid():
    """The Claude Code process this command runs under, or None."""
    pid = os.getppid()
    while pid > 1:
        fields = ps_fields(pid)
        if not fields:
            return None
        if os.path.basename(fields[2]) == "claude":
            return pid
        pid = fields[0]
    return None


def owner_alive():
    """False once the Claude Code session that started talk mode has gone, even if
    it crashed or its terminal was closed. Unknown owner: assume alive."""
    pid = read_pid(OWNER_PID)
    return pid is None or alive(pid)


def terminal_tty():
    """The terminal of the Claude Code session that owns talk mode (cached)."""
    try:
        return TERMINAL_TTY.read_text().strip()
    except OSError:
        pass
    owner = read_pid(OWNER_PID)
    fields = owner and ps_fields(owner)
    if fields and fields[1] not in ("??", "-"):
        TERMINAL_TTY.write_text("/dev/" + fields[1])
        return "/dev/" + fields[1]
    pid = os.getppid()
    while pid > 1:
        out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.split()
        if len(out) < 2:
            return ""
        if out[1] not in ("??", "-"):
            tty = "/dev/" + out[1]
            TERMINAL_TTY.write_text(tty)
            return tty
        pid = int(out[0])
    return ""


def write_title(text, tty=None):
    tty = tty or terminal_tty()
    if not tty or not text:
        return
    try:
        with open(tty, "w") as t:
            t.write(f"\033]0;{text}\007")
    except OSError:
        pass


def mark_title(transcript_path):
    title = session_title(transcript_path)
    if title or not TITLE.exists():
        TITLE.write_text(title)
    terminal_tty()                          # found from the hook, which runs under Claude Code
    refresh_title()


def refresh_title():
    if not ACTIVE.exists():
        return
    try:
        tty = TERMINAL_TTY.read_text().strip()
        title = TITLE.read_text().strip()
    except OSError:
        return
    write_title(f"{MIC} {title}" if title else MIC, tty)


def unmark_title():
    """Back to the plain title; Claude Code takes over again on its next update."""
    try:
        tty = TERMINAL_TTY.read_text().strip()
        title = TITLE.read_text().strip()
    except OSError:
        return
    write_title(title, tty)


def stop_voice():
    """What talking over Claude does with headphones: stop the voice and drop
    replies queued so far."""
    pid = read_pid(SPEAKING)
    if alive(pid):
        touch(BARGED)
        kill(pid)


def cmd_key(action):
    if not ACTIVE.exists():
        return 0
    if action == "quiet":
        stop_voice()
    elif action == "mute":
        if PAUSED.exists():
            PAUSED.unlink(missing_ok=True)
            cmd_reply("Listening.")
        else:
            PAUSED.touch()
            stop_voice()
            deadline = time.time() + 1.5    # else speak.py drops "Muted" with the stopped reply
            while BARGED.exists() and time.time() < deadline:
                time.sleep(0.05)
            cmd_reply("Muted.")
    return 0


def emit_context(event, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))


def cmd_hook():
    if not ACTIVE.exists():
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    # A dead listener without a stop request means talk mode crashed: clear the
    # state so no hook ever blocks or steers a session again.
    if not listener_alive() and not STOP_FLAG.exists():
        cmd_stop(quick=True, quiet=True)
        return 0

    owner = OWNER.read_text().strip() if OWNER.exists() else ""
    if not owner:
        # The session whose Bash call ran start.sh owns talk mode.
        if event == "PostToolUse" and ROOT.name in command and "scripts/start.sh" in command:
            OWNER.write_text(sid)
        return 0
    if sid != owner:
        return 0

    touch(HEARTBEAT)
    bin_dir = ROOT / "bin"

    if event == "PreToolUse" and not data.get("agent_id"):
        mark_title(data.get("transcript_path"))

    if event == "UserPromptSubmit":
        if (data.get("prompt") or "").lstrip().startswith("/talk"):
            return 0
        cmd_stop(quick=True, quiet=True)
        emit_context(event, "Talk mode was switched off because the user typed a "
                     "message, and the microphone is now closed. Answer in text as "
                     "usual; do not call next_turn.sh or reply.sh.")
        return 0

    if event == "PreToolUse":
        # After an interrupt phrase, refuse everything but talk mode's own scripts.
        if SCRIPT_MARK + "bin/" in command or SCRIPT_MARK + "scripts/" in command:
            return 0
        if not INTERRUPT.exists():
            label = step_label(data.get("tool_name", ""), tool_input)
            if label:
                STEP.write_text(label)
            return 0
        reason = ("The user interrupted this work by voice. Stop now and return what "
                  "you have so far.") if data.get("agent_id") else interrupt_note(bin_dir)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        return 0

    if event == "PostToolUse":
        if SCRIPT_MARK + "bin/next_turn.sh" in command or SCRIPT_MARK + "scripts/" in command:
            return 0
        if data.get("agent_id"):            # a subagent's tool call: leave it to the main loop
            return 0
        STEP.unlink(missing_ok=True)        # between tool calls Claude is thinking
        if INTERRUPT.exists():
            if SCRIPT_MARK + "bin/" not in command:   # reply.sh means it is being handled
                emit_context(event, interrupt_note(bin_dir))
            return 0
        if STOP_FLAG.exists():
            emit_context(event, "The user ended talk mode by voice. Say a one-sentence "
                         f"goodbye with {bin_dir}/reply.sh, then run "
                         f"{ROOT}/scripts/stop.sh and do not call next_turn.sh again.")
            return 0
        records = take()
        if records:
            emit_context(event, "While you were working, the user said (by voice): "
                         f"\"{format_records(records)}\". Treat this as their newest "
                         "instruction. If it changes or cancels what you are doing, "
                         "stop and adjust now, and acknowledge it in one short sentence "
                         f"with {bin_dir}/reply.sh.")
        return 0

    if event == "PermissionRequest":
        # Claude Code is about to show a prompt; make it audible, never answer it.
        play_cue("attention")
        cmd_reply("I need your approval on the screen.")
        return 0

    if event == "Stop":
        if STOP_FLAG.exists():
            return 0
        if INTERRUPT.exists():
            print(json.dumps({"decision": "block", "reason": interrupt_note(bin_dir)}))
            return 0
        print(json.dumps({"decision": "block", "reason": (
            "Talk mode is still on, so keep the voice loop going. If you have not "
            f"spoken your answer yet, run {bin_dir}/reply.sh with a short spoken "
            f"summary first. Then run {bin_dir}/next_turn.sh 540 with the Bash "
            "timeout set to 600000 and handle what the user says.")}))
        return 0
    return 0


def cmd_claim():
    """One talk mode at a time: refuse while another Claude Code session that is
    still running has it on. A crashed owner doesn't block (its pid is gone)."""
    me = claude_pid()
    other = read_pid(OWNER_PID)
    if ACTIVE.exists() and other and other != me and is_claude(other):
        fields = ps_fields(other)
        where = f" in terminal {fields[1]}" if fields and fields[1] not in ("??", "-") else ""
        log(f"Talk mode is already on in another Claude Code session (process {other}{where}). "
            "End it there first: say \"stop talk mode\", or run scripts/stop.sh.")
        return 3
    print(me or "")
    return 0


def cmd_stop(quick=False, quiet=False):
    """End talk mode. Without --quick, let a final spoken reply finish first."""
    ACTIVE.unlink(missing_ok=True)
    OWNER.unlink(missing_ok=True)
    unmark_title()
    kill(read_pid(LISTEN_PID))              # listener closes ffmpeg, i.e. the mic
    if not quick:
        time.sleep(0.5)                     # give speak.py time to pick up a goodbye
        deadline = time.time() + 20
        while SPEAKING.exists() and time.time() < deadline:
            time.sleep(0.1)
    kill(read_pid(SPEAKING))                # a `say` still talking
    kill(read_pid(SPEAK_PID))
    kill(read_pid(KEYS_PID))
    kill(read_pid(SERVER_PID))
    for p in (INBOX, OUTBOX, CURSOR, CURSOR_LOCK, STOP_FLAG, SPEAKING, BARGED, WORKING, INTERRUPT, STEP, CUE_AT, ON_SPEAKERS,
              PAUSED, KEYS_PID, TERMINAL_TTY, TITLE, OWNER_PID, LISTEN_PID, SPEAK_PID, SERVER_PID, HEARTBEAT, LAST_HEARD,
              RT / "listen.ready", RT / "listen.device", RT / "silent.aiff"):
        p.unlink(missing_ok=True)
    for wav in RT.glob("utt-*.wav"):
        wav.unlink(missing_ok=True)
    if not quiet:
        print("Talk mode is off. The microphone is closed.")
    return 0


def cmd_status():
    print(f"active:   {ACTIVE.exists()}")
    print(f"owner:    {OWNER.read_text().strip() if OWNER.exists() else '-'}")
    for name, path in (("listener", LISTEN_PID), ("speaker", SPEAK_PID),
                       ("keys", KEYS_PID), ("whisper", SERVER_PID)):
        pid = read_pid(path)
        print(f"{name + ':':9} {'running (pid %d)' % pid if alive(pid) else 'not running'}")
    if STOP_FLAG.exists():
        print(f"stop:     {STOP_FLAG.read_text().strip()}")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "wait":
        return cmd_wait(float(rest[0]) if rest else 100)
    if cmd == "reply":
        return cmd_reply(" ".join(rest))
    if cmd == "hook":
        return cmd_hook()
    if cmd == "claim":
        return cmd_claim()
    if cmd == "key" and rest:
        return cmd_key(rest[0])
    if cmd == "stop":
        return cmd_stop(quick="--quick" in rest)
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
