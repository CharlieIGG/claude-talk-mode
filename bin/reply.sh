#!/bin/bash
# Speak a short reply aloud. Usage: reply.sh "Done, the tests pass."
exec /usr/bin/python3 -I "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/talkctl.py" reply "$*"
