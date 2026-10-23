#!/bin/bash
# Claude Code hook (PostToolUse, Stop, UserPromptSubmit, PermissionRequest). Does nothing unless
# talk mode is on, so it costs other sessions only this file check.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/runtime/active" ] || exit 0
exec /usr/bin/python3 -I "$ROOT/bin/talkctl.py" hook
