#!/usr/bin/env python3
"""Shared state and commands for talk mode.

Subcommands:
  wait [SECONDS]   block until the user finishes an utterance, print it, exit
  reply TEXT       queue TEXT to be spoken aloud
  hook             Claude Code hook entry point (reads the hook JSON on stdin)
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
HEARTBEAT = RT / "heartbeat"        # touched whenever the owning session is active
LAST_HEARD = RT / "last_heard"      # touched whenever the user says something
STOP_FLAG = RT / "stop"             # contains the reason the session should end
SPEAKING = RT / "speaking.lock"     # contains the pid of the running `say`
BARGED = RT / "barged"              # the user talked over the speaker
WORKING = RT / "working"            # Claude is handling a turn (not listening)
LISTEN_PID = RT / "listen.pid"
SPEAK_PID = RT / "speak.pid"
SERVER_PID = RT / "server.pid"      # only present if start.sh started the server

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

    if event == "UserPromptSubmit":
        if (data.get("prompt") or "").lstrip().startswith("/talk"):
            return 0
        cmd_stop(quick=True, quiet=True)
        emit_context(event, "Talk mode was switched off because the user typed a "
                     "message, and the microphone is now closed. Answer in text as "
                     "usual; do not call next_turn.sh or reply.sh.")
        return 0

    if event == "PostToolUse":
        if SCRIPT_MARK + "bin/next_turn.sh" in command or SCRIPT_MARK + "scripts/" in command:
            return 0
        if data.get("agent_id"):            # a subagent's tool call: leave it to the main loop
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
        print(json.dumps({"decision": "block", "reason": (
            "Talk mode is still on, so keep the voice loop going. If you have not "
            f"spoken your answer yet, run {bin_dir}/reply.sh with a short spoken "
            f"summary first. Then run {bin_dir}/next_turn.sh 540 with the Bash "
            "timeout set to 600000 and handle what the user says.")}))
        return 0
    return 0


def cmd_stop(quick=False, quiet=False):
    """End talk mode. Without --quick, let a final spoken reply finish first."""
    ACTIVE.unlink(missing_ok=True)
    OWNER.unlink(missing_ok=True)
    kill(read_pid(LISTEN_PID))              # listener closes ffmpeg, i.e. the mic
    if not quick:
        time.sleep(0.5)                     # give speak.py time to pick up a goodbye
        deadline = time.time() + 20
        while SPEAKING.exists() and time.time() < deadline:
            time.sleep(0.1)
    kill(read_pid(SPEAKING))                # a `say` still talking
    kill(read_pid(SPEAK_PID))
    kill(read_pid(SERVER_PID))
    for p in (INBOX, OUTBOX, CURSOR, CURSOR_LOCK, STOP_FLAG, SPEAKING, BARGED, WORKING,
              LISTEN_PID, SPEAK_PID, SERVER_PID, HEARTBEAT, LAST_HEARD,
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
                       ("whisper", SERVER_PID)):
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
    if cmd == "stop":
        return cmd_stop(quick="--quick" in rest)
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
