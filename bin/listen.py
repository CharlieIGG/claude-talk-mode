#!/usr/bin/env python3
"""Microphone -> silence detection -> local Whisper server -> runtime/inbox.jsonl.

The mic is read through ffmpeg (avfoundation), so no Python audio packages are
needed. A turn ends after a pause. When barge-in is on (headphones), speaking
over the assistant for a moment stops its voice and drops queued replies.
Saying a stop phrase closes the mic right away; an interrupt phrase stops
Claude at its next tool call.

Usage:  listen.py              live microphone (device from config.json)
        listen.py --file X.wav feed a recording instead (for testing)
"""
import array
import json
import math
import os
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import talkctl as tc  # noqa: E402

SR = 16000
FRAME_MS = 30
FRAME_BYTES = SR * FRAME_MS // 1000 * 2
CALIBRATE_FRAMES = 20                      # ~0.6 s of room noise
RECALIBRATE_FRAMES = 167                   # ~5 s of quiet between turns, rechecked every second
SETTLE_FRAMES = 33                         # ~1 s ignored while a Bluetooth mic switches on
STALL_S = 2.0                              # no audio for this long: reopen the mic
PREROLL_FRAMES = 10                        # ~0.3 s kept before speech starts

# Whisper tends to invent these in silence or noise; drop them as whole turns.
# Short real answers ("no", "ok", "yeah") must survive, so nothing else is dropped.
JUNK = {"you", "thank you", "thanks for watching", "uh", "um", "hmm", "mm"}


def rms(frame):
    a = array.array("h")
    a.frombytes(frame)
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0


def clean(text):
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)   # [BLANK_AUDIO], (music)
    return " ".join(text.split())


def is_junk(text):
    t = text.lower().strip(" .,!?-")
    return not t or t in JUNK or not re.search(r"[a-z0-9]", t)   # e.g. a lone "♪"


def normalize(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


COMMAND_EXTRA_WORDS = 3   # "okay, stop talk mode please" still counts; a long sentence does not


def said_alone(text, phrases):
    """True when the utterance is one of the phrases on its own (repeats allowed),
    give or take a few words, so mentioning a phrase inside a longer sentence
    does not trigger it."""
    for phrase in phrases:
        rest = f" {normalize(text)} "
        if f" {phrase} " not in rest:
            continue
        while f" {phrase} " in rest:
            rest = rest.replace(f" {phrase} ", " ")
        if len(rest.split()) <= COMMAND_EXTRA_WORDS:
            return True
    return False


CUE_ECHO_S = 2.0                           # a feedback sound (padded) lasts about this long
OUTPUT_CHECK_S = 5                         # how often the output device is looked up


def audio_devices():
    proc = subprocess.run(["system_profiler", "SPAudioDataType", "-json"],
                          capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)["SPAudioDataType"][0]["_items"]
    except (ValueError, KeyError, IndexError):
        return []


def output_on_headphones():
    """(device name, True if it is headphones) for the current macOS output. Bluetooth
    and anything named headphones/headset counts; built-in or other speakers do not."""
    for dev in audio_devices():
        if dev.get("coreaudio_default_audio_output_device") == "spaudio_yes":
            name = dev.get("_name", "")
            bluetooth = "bluetooth" in dev.get("coreaudio_device_transport", "")
            return name, bluetooth or any(w in name.lower() for w in ("headphone", "headset"))
    return "unknown", False


def input_device(cfg):
    """The configured microphone, or with "default" whatever macOS currently uses
    as input (e.g. the earbuds while they are worn)."""
    name = cfg["audio"].get("input_device") or "default"
    if name != "default":
        return name
    for dev in audio_devices():
        if dev.get("coreaudio_default_audio_input_device") == "spaudio_yes":
            return dev.get("_name", "0")
    return "0"                                         # first audio input device


def open_source(cfg, file_path):
    if file_path:
        src = ["-re", "-i", file_path]                 # real-time pace, like a mic
    else:
        device = input_device(cfg)
        (tc.RT / "listen.device").write_text(device)
        tc.log(f"[listen] microphone: {device}")
        src = ["-f", "avfoundation", "-i", ":" + device]
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *src,
         "-ar", str(SR), "-ac", "1", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)


STALLED = b""


def read_frame(stream, timeout=None):
    """One 30 ms frame; None at end of input; STALLED if nothing arrives in time
    (Bluetooth microphones can freeze while switching into call mode)."""
    buf = b""
    while len(buf) < FRAME_BYTES:
        if timeout and not select.select([stream], [], [], timeout)[0]:
            return STALLED
        chunk = os.read(stream.fileno(), FRAME_BYTES - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def transcribe(frames, cfg):
    path = tc.RT / f"utt-{uuid.uuid4().hex}.wav"
    try:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(b"".join(frames))
        url = f"http://127.0.0.1:{cfg['stt']['port']}/inference"
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "30", url, "-F", f"file=@{path}",
             "-F", "response_format=json", "-F", "temperature=0"],
            capture_output=True, text=True)
        try:
            return clean(json.loads(proc.stdout).get("text") or "")
        except ValueError:
            tc.log(f"[listen] transcription failed: {proc.stdout[:120]} {proc.stderr[:120]}")
            return ""
    finally:
        path.unlink(missing_ok=True)


def main(argv):
    file_path = argv[argv.index("--file") + 1] if "--file" in argv else None
    cfg = tc.load_config()
    vad = cfg["vad"]
    barge_setting = cfg.get("barge_in", "auto")
    # barge[0]: talking over Claude stops its voice. Only safe with headphones; on
    # speakers the mic hears Claude's own voice, so it is ignored instead.
    barge = [barge_setting is True]
    stop_phrases = [normalize(p) for p in cfg.get("stop_phrases", [])]
    interrupt_phrases = [normalize(p) for p in cfg.get("interrupt_phrases", [])]
    idle_s = cfg["session"]["idle_minutes"] * 60
    silence_frames = max(1, vad["silence_ms"] // FRAME_MS)
    min_speech_frames = max(1, vad["min_speech_ms"] // FRAME_MS)
    barge_frames = max(1, vad["barge_in_ms"] // FRAME_MS)
    tc.RT.mkdir(parents=True, exist_ok=True)
    managed = tc.ACTIVE.exists() or not file_path   # under start.sh, not a bare test

    proc = open_source(cfg, file_path)
    stopping = threading.Event()

    def watch_output():
        """With barge_in "auto", follow the output device (earbuds come and go)."""
        last = None
        while not stopping.is_set():
            name, phones = output_on_headphones()
            barge[0] = phones
            if phones:
                tc.ON_SPEAKERS.unlink(missing_ok=True)
            else:
                tc.touch(tc.ON_SPEAKERS)
            if (name, phones) != last:
                tc.log(f"[listen] output: {name}, talking over Claude "
                       + ("stops its voice" if phones else "is off (speakers, echo protection)"))
                last = (name, phones)
            stopping.wait(OUTPUT_CHECK_S)
    if barge_setting == "auto" and not file_path:
        threading.Thread(target=watch_output, daemon=True).start()

    def on_term(signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)

    # Transcription runs on a worker so the mic keeps being read meanwhile.
    jobs = queue.Queue()

    def worker():
        while True:
            job = jobs.get()
            if job is None:
                return
            frames, interrupted = job
            text = transcribe(frames, cfg)
            if not text or is_junk(text):
                tc.log("[listen] turn discarded as noise")
                continue
            if said_alone(text, stop_phrases):
                tc.request_stop("stop-phrase")
                tc.log("[listen] stop phrase heard, closing the microphone")
                stopping.set()
                return
            # While Claude is busy, an interrupt phrase makes the hooks refuse its
            # next tool call instead of waiting in the inbox (see talkctl.cmd_hook).
            if said_alone(text, interrupt_phrases) and tc.ACTIVE.exists() \
                    and tc.WORKING.exists():
                tc.INTERRUPT.write_text(text)
                tc.play_cue("interrupt", cfg)
                tc.touch(tc.LAST_HEARD)
                tc.log("[listen] interrupt phrase heard, Claude's next tool call is refused")
                continue
            rec = {"ts": time.time(), "text": text, "interrupted": interrupted}
            with open(tc.INBOX, "a") as f:
                f.write(json.dumps(rec) + "\n")
            tc.touch(tc.LAST_HEARD)
            tc.log(f"[listen] utterance: {len(text)} chars")
    transcriber = threading.Thread(target=worker, daemon=True)
    transcriber.start()

    try:
        if not file_path:                                  # skip the switch-on noise
            for _ in range(SETTLE_FRAMES):
                read_frame(proc.stdout, 5)
        noise = [rms(f) for f in (read_frame(proc.stdout) for _ in range(CALIBRATE_FRAMES)) if f]
        floor = sum(noise) / len(noise) if noise else 0.0
        threshold = max(vad["min_threshold"], floor * vad["noise_multiplier"])
        (tc.RT / "listen.ready").write_text(f"{threshold:.4f}")
        tc.log(f"[listen] ready: noise floor {floor:.4f}, threshold {threshold:.4f}")
        # The start-up measurement can catch a noisy moment (or the user already
        # talking) and leave the threshold too high for the whole session. So the
        # room is re-measured from the quiet between turns: the 10th percentile
        # ignores brief sounds, and the threshold follows the room up and down.
        quiet = []

        preroll, voiced = [], []
        triggered, interrupted = False, False
        speech_frames, silence = 0, 0
        n = 0
        loudest = 0.0
        while not stopping.is_set():
            frame = read_frame(proc.stdout, None if file_path else STALL_S)
            if frame is STALLED:
                tc.log(f"[listen] no audio for {STALL_S:.0f}s, reopening the microphone")
                proc.terminate()
                proc.wait()
                proc = open_source(cfg, file_path)
                if triggered:
                    tc.log("[listen] turn dropped by the stall")
                preroll, voiced, triggered, speech_frames, silence = [], [], False, 0, 0
                continue
            if frame is None:
                break
            n += 1
            if managed and n % 33 == 0:                    # about once a second
                if not tc.ACTIVE.exists():
                    break
                if tc.age(tc.HEARTBEAT) > idle_s:
                    tc.log("[listen] Claude Code has been quiet too long, stopping")
                    tc.request_stop("idle")
                    break

            if tc.PAUSED.exists():                         # microphone key: not listening
                preroll, voiced, triggered, speech_frames, silence = [], [], False, 0, 0
                continue
            speaking = tc.SPEAKING.exists()
            barge_in = barge[0]
            if speaking and not barge_in:                  # speakers: ignore our own voice
                preroll, voiced, triggered, speech_frames, silence = [], [], False, 0, 0
                continue
            if not barge_in and not triggered and tc.age(tc.CUE_AT) < CUE_ECHO_S:
                continue                                   # ...and our own sounds

            level = rms(frame)
            loudest = max(loudest, level)
            loud = level >= threshold
            if not triggered:
                quiet = (quiet + [level])[-RECALIBRATE_FRAMES:]
                if n % 33 == 0 and len(quiet) == RECALIBRATE_FRAMES:
                    floor = sorted(quiet)[RECALIBRATE_FRAMES // 10]
                    new = max(vad["min_threshold"], floor * vad["noise_multiplier"])
                    if abs(new - threshold) > 0.2 * threshold:
                        tc.log(f"[listen] room re-measured: noise floor {floor:.4f}, "
                               f"threshold {threshold:.4f} -> {new:.4f}")
                        threshold = new
                preroll = (preroll + [frame])[-PREROLL_FRAMES:]
                if loud:
                    triggered, voiced, preroll = True, preroll, []
                    speech_frames, silence, interrupted = 1, 0, False
                    tc.log("[listen] turn started")
                continue

            voiced.append(frame)
            if loud:
                speech_frames += 1
                silence = 0
                if speaking and not interrupted and speech_frames >= barge_frames:
                    tc.touch(tc.BARGED)
                    tc.kill(tc.read_pid(tc.SPEAKING))      # stop the voice mid-sentence
                    interrupted = True
                    tc.log("[listen] user talked over the reply, voice stopped")
            else:
                silence += 1
                if silence >= silence_frames:
                    tc.log(f"[listen] turn ended after {len(voiced) * FRAME_MS / 1000:.1f}s"
                           + ("" if speech_frames >= min_speech_frames else " (too short, ignored)"))
                    if speech_frames >= min_speech_frames:
                        tc.play_cue("heard")               # instant "got it"; rereads config
                        jobs.put((voiced, interrupted))
                    triggered, voiced, speech_frames, silence = False, [], 0, 0
        if file_path and triggered and speech_frames >= min_speech_frames:
            jobs.put((voiced, interrupted))                # recording ended mid-turn
    finally:
        proc.terminate()                                   # releases the microphone
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        jobs.put(None)
        if "loudest" in locals():
            tc.log(f"[listen] loudest sound {loudest:.4f} (speech counts from {threshold:.4f})")
        tc.log("[listen] microphone closed")
    if file_path:                                          # let queued turns finish
        transcriber.join(timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
