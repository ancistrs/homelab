#!/usr/bin/env bash
# Rebuild all homelab Python venvs after a pyenv upgrade.
# Usage: ./scripts/rebuild-venvs.sh [python-path]
set -euo pipefail

# Ensure pyenv shims are in PATH (pyenv init lives in .zshrc,
# which non-interactive bash scripts don't source).
if [[ -d "$HOME/.pyenv/shims" ]]; then
  export PATH="$HOME/.pyenv/shims:$PATH"
fi

PYTHON="${1:-python3}"

echo "Using: $($PYTHON --version)"

HOMELAB="$(cd "$(dirname "$0")/.." && pwd)"

NAMES=(kb-sync whisper)
REQS=(scripts/kb-sync/requirements.txt whisper/requirements.txt)

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  req="$HOMELAB/${REQS[$i]}"
  venv="$HOME/.venvs/$name"
  echo ""
  echo "── $name ──"
  if [[ ! -f "$req" ]]; then
    echo "  SKIP: $req not found"
    continue
  fi
  rm -rf "$venv"
  $PYTHON -m venv "$venv"
  "$venv/bin/pip" install -q -r "$req"
  echo "  OK: $("$venv/bin/python" --version), $(wc -l < "$req" | tr -d ' ') deps"
done

echo ""
echo "Restarting services..."
launchctl kickstart -k "gui/$(id -u)/homelab.kb-query-server"
launchctl kickstart -k "gui/$(id -u)/user.whisper"

echo ""
echo "Waiting for services..."
SERVICES=("kb-query:8100" "whisper:8000")
MAX_WAIT=60
for svc in "${SERVICES[@]}"; do
  name="${svc%%:*}"
  port="${svc##*:}"
  elapsed=0
  while [[ $elapsed -lt $MAX_WAIT ]]; do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo "  $name: ok (${elapsed}s)"
      break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  if [[ $elapsed -ge $MAX_WAIT ]]; then
    echo "  $name: FAIL (no response after ${MAX_WAIT}s)"
  fi
done

echo ""
echo "Done."
