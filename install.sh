#!/bin/bash
# Connect talk mode to Claude Code: installs the /talk skill, adds the hook and
# four allowed commands to ~/.claude/settings.json (after a backup). Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
HOOK="$ROOT/hooks/talk-hook.sh"
MODEL="$ROOT/models/ggml-small.en.bin"

echo "Checking prerequisites:"
missing=0
for b in whisper-server ffmpeg jq say curl; do
  if command -v "$b" >/dev/null; then echo "  ok  $b"; else echo "  MISSING  $b"; missing=1; fi
done
[ "$missing" = 0 ] || { echo "Install the missing tools first (brew install whisper-cpp ffmpeg jq)."; exit 1; }

if [ ! -f "$MODEL" ]; then
  echo "Downloading the Whisper model (466 MB) and checking its fingerprint ..."
  mkdir -p "$ROOT/models"
  expected="$(curl -s "https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main" \
    | jq -r '.[] | select(.path=="ggml-small.en.bin") | .lfs.oid')"
  curl -sL -o "$MODEL.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
  actual="$(shasum -a 256 "$MODEL.part" | cut -d' ' -f1)"
  [ -n "$expected" ] && [ "$expected" = "$actual" ] || { rm -f "$MODEL.part"; echo "Fingerprint mismatch, model not installed."; exit 1; }
  mv "$MODEL.part" "$MODEL"
fi
echo "  ok  Whisper model"

chmod +x "$ROOT"/bin/*.sh "$ROOT"/bin/*.py "$ROOT"/scripts/*.sh "$ROOT"/hooks/*.sh

mkdir -p "$HOME/.claude/skills/talk"
sed "s|__HF__|$ROOT|g" "$ROOT/skill/SKILL.md" > "$HOME/.claude/skills/talk/SKILL.md"
echo "  ok  /talk skill installed in ~/.claude/skills/talk"

[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
backup="$SETTINGS.bak-talkmode-$(date +%Y%m%d-%H%M%S)"
cp "$SETTINGS" "$backup"
rules="$(jq -n --arg r "$ROOT" '[
  "Bash(\($r)/scripts/start.sh:*)", "Bash(\($r)/scripts/stop.sh:*)",
  "Bash(\($r)/bin/next_turn.sh:*)", "Bash(\($r)/bin/reply.sh:*)"]')"
tmp="$(mktemp)"
jq --arg cmd "$HOOK" --argjson rules "$rules" '
  def addhook(ev):
    .hooks[ev] = ((.hooks[ev] // [])
      | if any(.[]; any(.hooks[]?; .command == $cmd)) then .
        else . + [{"hooks": [{"type": "command", "command": $cmd, "timeout": 10}]}] end);
  .hooks = (.hooks // {})
  | addhook("PreToolUse") | addhook("PostToolUse") | addhook("Stop") | addhook("UserPromptSubmit")
  | addhook("PermissionRequest")
  | .permissions = (.permissions // {})
  | .permissions.allow = ((.permissions.allow // []) as $a
      | $a + [$rules[] | select(. as $r | $a | index($r) | not)])
' "$SETTINGS" > "$tmp"
jq empty "$tmp"
mv "$tmp" "$SETTINGS"
echo "  ok  hook and allowed commands added to $SETTINGS"
echo "      (backup: $backup)"
echo
echo "Done. Start a new Claude Code session, put on headphones, and type /talk."
