#!/usr/bin/env bash
# Wrapper for the homelab.logrotate LaunchAgent.
# Quiet on no-op runs (logrotate prints nothing unless something rotates).
set -euo pipefail
export PATH="/opt/homebrew/sbin:/opt/homebrew/bin:/usr/bin:/bin"

CONF="/Users/fink/PAOS/code/homelab/scripts/logrotate/homelab.conf"
STATE="/Users/fink/PAOS/code/homelab/scripts/logrotate/logrotate.state"

exec /opt/homebrew/sbin/logrotate --state "$STATE" "$CONF"
