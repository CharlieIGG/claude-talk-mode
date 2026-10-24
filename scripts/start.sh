#!/bin/bash
# Start talk mode: local Whisper server (127.0.0.1 only), listener, speaker.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="$ROOT/runtime"
PY=(/usr/bin/python3 -I)
cd "$ROOT"

for b in whisper-server ffmpeg say curl jq; do
  command -v "$b" >/dev/null || { echo "Missing: $b (see README.md)" >&2; exit 1; }
done

"${PY[@]}" bin/talkctl.py stop --quick >/dev/null 2>&1 || true   # clear any old session
mkdir -p "$RT" logs
chmod 700 "$RT"

PORT="$(jq -r .stt.port config.json)"
MODEL="$(jq -r .stt.model config.json)"
LANGUAGE="$(jq -r .stt.language config.json)"
[ -f "$MODEL" ] || { echo "Whisper model not found: $MODEL (see README.md)" >&2; exit 1; }

if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then
  nohup whisper-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" -l "$LANGUAGE" -nt -sns \
    </dev/null >>logs/whisper.log 2>&1 &
  echo $! > "$RT/server.pid"
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && break
    sleep 0.5
  done
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/" || { echo "Whisper server did not start, see logs/whisper.log" >&2; exit 1; }
fi

: > "$RT/inbox.jsonl"
: > "$RT/outbox.jsonl"
echo 0 > "$RT/inbox.cursor"
rm -f "$RT/stop" "$RT/interrupt" "$RT/barged" "$RT/speaking.lock" "$RT/owner_session" "$RT/listen.ready"
touch "$RT/heartbeat" "$RT/last_heard"

nohup "${PY[@]}" bin/listen.py </dev/null >>logs/listen.log 2>&1 &
echo $! > "$RT/listen.pid"
nohup "${PY[@]}" bin/speak.py </dev/null >>logs/speak.log 2>&1 &
echo $! > "$RT/speak.pid"

for _ in $(seq 1 100); do
  [ -f "$RT/listen.ready" ] && break
  kill -0 "$(cat "$RT/listen.pid")" 2>/dev/null || break
  sleep 0.1
done
if [ ! -f "$RT/listen.ready" ]; then
  "${PY[@]}" bin/talkctl.py stop --quick >/dev/null 2>&1 || true
  echo "The microphone could not be opened, see logs/listen.log. Does your terminal app have microphone access (System Settings > Privacy & Security > Microphone)?" >&2
  exit 1
fi
touch "$RT/active"     # only now, so hooks never see "active" without a listener
echo "Talk mode is on. Microphone: $(cat "$RT/listen.device" 2>/dev/null || echo unknown), speech threshold $(cat "$RT/listen.ready")."
# macOS keeps an input volume per microphone, and apps (e.g. Teams) can lower it;
# at 21% the built-in mic missed normal speech from a few steps away.
level="$(osascript -e 'input volume of (get volume settings)' 2>/dev/null || true)"
if [ -n "$level" ] && [ "$level" != "missing value" ] && [ "$level" -lt 50 ]; then
  echo "Warning: the microphone's input volume is only $level%, so quiet or distant speech may be missed. Raise it in System Settings > Sound > Input (or: osascript -e 'set volume input volume 80')."
fi
