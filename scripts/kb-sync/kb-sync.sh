#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="unix:///Users/fink/.orbstack/run/docker.sock"
exec /Users/fink/.venvs/kb-sync/bin/python /Users/fink/PAOS/code/homelab/scripts/kb-sync/kb_sync.py
