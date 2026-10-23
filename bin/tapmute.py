#!/usr/bin/env python3
"""Double tap on Bluetooth earbuds = mute / unmute the Mac's microphone, for all apps.

Earbuds only send the headset "voice recognition" command while a microphone is in
use (call mode); otherwise a double tap stays "next track". So this only reacts while
a microphone is active. Siri must be off on the Mac, or it opens on every double tap.

Muting sets the Mac's input volume to 0, which tested as complete silence for the
earbuds' microphone. The previous level is kept in state/mute.json and restored on
unmute, and also when this watcher starts (so a reboot never leaves you muted).

Standard library only; runs on macOS's /usr/bin/python3.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "mute.json"
SIGNAL = "Received End Voice Command"
PREDICATE = f'process == "bluetoothd" AND eventMessage CONTAINS "{SIGNAL}"'
DEBOUNCE_S = 1.0


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def input_volume():
    out = subprocess.run(["osascript", "-e", "input volume of (get volume settings)"],
                         capture_output=True, text=True).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return None


def set_input_volume(level):
    subprocess.run(["osascript", "-e", f"set volume input volume {int(level)}"])


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"muted": False, "level": 100}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def announce(text):
    try:
        tts = json.loads((ROOT / "config.json").read_text())["tts"]
    except (OSError, ValueError, KeyError):
        tts = {}
    cmd = ["say", "-r", str(tts.get("rate", 200))]
    if tts.get("voice"):
        cmd += ["-v", tts["voice"]]
    # Trailing silence: Bluetooth lags, and `say` would otherwise cut the word off.
    subprocess.Popen(cmd + [text + " [[slnc 800]]"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def toggle():
    state = load_state()
    current = input_volume()
    if state.get("muted") and current == 0:
        set_input_volume(state.get("level") or 100)
        state["muted"] = False
        announce("Microphone on.")
        log("unmuted")
    else:
        state["level"] = current if current else 100
        set_input_volume(0)
        state["muted"] = True
        announce("Muted.")
        log("muted")
    save_state(state)


def main():
    state = load_state()
    if state.get("muted"):                       # never start up silently muted
        set_input_volume(state.get("level") or 100)
        state["muted"] = False
        save_state(state)
        log("restored the microphone level left muted by a previous run")
    last = 0.0
    while True:
        log("watching for earbud double taps")
        proc = subprocess.Popen(["/usr/bin/log", "stream", "--style", "compact",
                                 "--predicate", PREDICATE],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            if SIGNAL not in line:
                continue
            now = time.monotonic()
            if now - last < DEBOUNCE_S:
                continue
            last = now
            toggle()
        log("log stream ended, restarting in 2 seconds")
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
