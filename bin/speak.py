#!/usr/bin/env python3
"""Speak queued replies (runtime/outbox.jsonl) with macOS `say`.

While speaking, runtime/speaking.lock holds the `say` pid so the listener can
stop it when the user talks over it; queued replies are then dropped.

Also cleans up an abandoned session: if the listener has gone (stop phrase or
idle) and nobody ran stop.sh within 90 seconds, it shuts talk mode down.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import talkctl as tc  # noqa: E402


def speakable(text):
    """Strip things that sound bad read aloud (code, markdown, links)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[`*_#>|]", "", text)
    return " ".join(text.split())


def say(text, tts):
    cmd = ["say", "-r", str(tts.get("rate", 190)), "-f", "-"]
    if tts.get("voice"):
        cmd[1:1] = ["-v", tts["voice"]]
    if os.environ.get("HF_SILENT"):                       # tests: render, don't play
        cmd[1:1] = ["-o", str(tc.RT / "silent.aiff")]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    tc.SPEAKING.write_text(str(p.pid))
    # Trailing silence keeps the audio stream open until Bluetooth headphones,
    # which lag behind, have played the last words; otherwise they get cut off.
    # Speakers don't lag, and there the mic is deaf while we speak (echo
    # protection), so padding would only make the user wait to answer.
    speakers = tc.ON_SPEAKERS.exists()
    try:
        p.communicate((text + ("" if speakers else " [[slnc 1000]]")).encode())
    finally:
        time.sleep(0.1 if speakers else 0.25)              # let the audio tail decay
        tc.SPEAKING.unlink(missing_ok=True)
    return p.returncode


def tick_due(every, last_tick, last_spoke):
    """True when a "still working" tick should play: Claude is handling a turn,
    nothing is being said, and nothing was heard or spoken for `every` seconds.
    Stops after 2 minutes without any hook activity (e.g. the user pressed Esc)."""
    if not tc.WORKING.exists() or tc.SPEAKING.exists() or tc.STOP_FLAG.exists():
        return False
    if tc.age(tc.HEARTBEAT) > 120:
        return False
    now = time.time()
    quiet_since = max(now - tc.age(tc.WORKING), last_spoke)
    return now - quiet_since >= every and now - last_tick >= every


def status_due(every, last_status, last_spoke):
    """True when a spoken "still working" update is due: Claude has been handling
    a turn for `every` seconds without saying anything. A long tool call keeps it
    going; without one, it stops 2 minutes after the last hook activity (e.g. the
    user pressed Esc), and after 20 minutes in any case."""
    if not every or not tc.WORKING.exists() or tc.SPEAKING.exists() or tc.STOP_FLAG.exists():
        return False
    quiet_hooks = tc.age(tc.HEARTBEAT)
    if quiet_hooks > 20 * 60 or (quiet_hooks > 120 and not tc.STEP.exists()):
        return False
    now = time.time()
    return now - max(now - tc.age(tc.WORKING), last_spoke, last_status) >= every


def status_text():
    try:
        step = tc.STEP.read_text().strip()
    except OSError:
        step = ""
    if not step:
        return "Still working, thinking it through."
    return f"Still working. Right now: {step[0].lower() + step[1:]}."


def main():
    cfg = tc.load_config()
    tts = cfg["tts"]
    every = cfg.get("cues", {}).get("working_every_s", 4)
    status_every = cfg["session"].get("status_every_s", 120)
    tc.OUTBOX.touch(exist_ok=True)
    pos = tc.OUTBOX.stat().st_size
    tc.log(f"[speak] ready (voice: {tts.get('voice') or 'system default'})")
    started = time.time()
    last_tick = last_spoke = last_status = 0.0

    def keep_title():                                      # 🎙 in the tab title, see talkctl
        while True:
            tc.refresh_title()
            time.sleep(0.3)
    threading.Thread(target=keep_title, daemon=True).start()
    while True:
        if status_due(status_every, last_status, last_spoke):
            say(speakable(status_text()), tts)
            last_status = last_spoke = time.time()
        if tick_due(every, last_tick, last_spoke):
            tc.play_cue("working", cfg)
            last_tick = time.time()
        if time.time() - started > 10 and not tc.listener_alive():
            if tc.age(tc.STOP_FLAG) > 90 or not tc.STOP_FLAG.exists() or not tc.owner_alive():
                tc.log("[speak] listener gone, shutting talk mode down")
                tc.cmd_stop(quick=True, quiet=True)
                return 0
        size = tc.OUTBOX.stat().st_size if tc.OUTBOX.exists() else 0
        if size < pos:
            pos = 0
        if size > pos:
            with open(tc.OUTBOX) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            tc.BARGED.unlink(missing_ok=True)              # stale: nothing was speaking
            for line in chunk.splitlines():
                try:
                    text = speakable(json.loads(line).get("text") or "")
                except ValueError:
                    continue
                if text and say(text, tts) not in (0, None) and not tc.BARGED.exists():
                    tc.log("[speak] voice failed, retrying with the system voice")
                    say(text, {"rate": tts.get("rate", 190)})
                last_spoke = time.time()
                if tc.BARGED.exists():
                    # The user talked over us: drop everything queued until now.
                    # Replies Claude queues after hearing the user still play.
                    pos = tc.OUTBOX.stat().st_size
                    tc.BARGED.unlink(missing_ok=True)
                    break
        time.sleep(0.15)


if __name__ == "__main__":
    sys.exit(main())
