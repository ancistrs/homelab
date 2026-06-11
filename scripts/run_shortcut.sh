#!/bin/bash
set -euo pipefail

LOG=/tmp/run_shortcut.log
echo "=== $(date) ===" >> "$LOG"
echo "ARGS: $@" >> "$LOG"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <shortcut_name>" >&2
    exit 1
fi

SHORTCUT_NAME="$1"

if [ -t 0 ]; then
    echo "NO STDIN" >> "$LOG"
    shortcuts run "$SHORTCUT_NAME"
else
    TMPFILE=$(mktemp -t shortcut).json
    cat > "$TMPFILE"
    echo "TMPFILE: $TMPFILE" >> "$LOG"
    echo "CONTENT:" >> "$LOG"
    cat "$TMPFILE" >> "$LOG"
    echo "" >> "$LOG"
    shortcuts run "$SHORTCUT_NAME" --input-path "$TMPFILE"
    RC=$?
    rm -f "$TMPFILE"
    exit $RC
fi