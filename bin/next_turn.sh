#!/bin/bash
# Wait for the user's next spoken turn and print it. Usage: next_turn.sh [SECONDS]
exec /usr/bin/python3 -I "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/talkctl.py" wait "${1:-100}"
