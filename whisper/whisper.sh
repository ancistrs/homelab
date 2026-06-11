#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
exec /Users/fink/.venvs/whisper/bin/python -m uvicorn \
  server:app --host 0.0.0.0 --port 8000 --app-dir "/Users/fink/PAOS/code/homelab/whisper"
