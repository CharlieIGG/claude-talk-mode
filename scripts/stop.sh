#!/bin/bash
# End talk mode: close the microphone, stop the voice and the Whisper server.
exec /usr/bin/python3 -I "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/talkctl.py" stop "$@"
