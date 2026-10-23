#!/bin/bash
# Disconnect talk mode from Claude Code. Leaves this folder (and the model) in place.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
HOOK="$ROOT/hooks/talk-hook.sh"

"$ROOT/scripts/stop.sh" --quick >/dev/null 2>&1 || true
rm -rf "$HOME/.claude/skills/talk"
echo "  ok  /talk skill removed"

if [ -f "$SETTINGS" ]; then
  backup="$SETTINGS.bak-talkmode-$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "$backup"
  tmp="$(mktemp)"
  jq --arg cmd "$HOOK" --arg root "$ROOT" '
    if .hooks then .hooks |= with_entries(
        .value |= map(select(any(.hooks[]?; .command == $cmd) | not))
        | select(.value | length > 0)) else . end
    | if .permissions.allow then .permissions.allow |= map(select(startswith("Bash(" + $root + "/") | not)) else . end
  ' "$SETTINGS" > "$tmp"
  jq empty "$tmp"
  mv "$tmp" "$SETTINGS"
  echo "  ok  hook and allowed commands removed (backup: $backup)"
fi
echo "Done. To remove everything, also delete $ROOT (and: brew uninstall whisper-cpp)."
