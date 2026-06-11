#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
exec /Users/fink/.venvs/kb-sync/bin/python -m uvicorn \
  kb_query:app --host 0.0.0.0 --port 8100 --app-dir "/Users/fink/PAOS/code/homelab/scripts/kb-sync"
