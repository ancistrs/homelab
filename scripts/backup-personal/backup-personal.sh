#!/bin/bash
set -euo pipefail
umask 077

log(){ printf '[%s] [personal] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

RESTIC_ENV="$HOME/.restic/personal.env"
RESTIC_EXCLUDE_COMMON="$HOME/.restic/excludes/common.txt"
RESTIC_EXCLUDE_PERSONAL="$HOME/.restic/excludes/personal.txt"

[ -f "$RESTIC_ENV" ] || { log "Restic env not found: $RESTIC_ENV"; exit 1; }
[ -f "$RESTIC_EXCLUDE_COMMON" ] || { log "Exclude file not found: $RESTIC_EXCLUDE_COMMON"; exit 1; }
[ -f "$RESTIC_EXCLUDE_PERSONAL" ] || { log "Exclude file not found: $RESTIC_EXCLUDE_PERSONAL"; exit 1; }
command -v restic >/dev/null 2>&1 || { log "restic not installed"; exit 1; }

. "$RESTIC_ENV"

SOURCES=(
  "$HOME/Documents"
  "$HOME/Desktop"
  "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Downloads"
  "$HOME/Pictures"
  "$HOME/PAOS"
  "$HOME/Library/Messages"
  "$HOME/Downloads"
  "$HOME/.restic"
)

EXISTING_SOURCES=()
for p in "${SOURCES[@]}"; do
  if [ -e "$p" ]; then
    EXISTING_SOURCES+=("$p")
  else
    log "SKIP missing: $p"
  fi
done

[ "${#EXISTING_SOURCES[@]}" -gt 0 ] || { log "No valid backup sources found."; exit 1; }

log "##### [START] BACKUP-PERSONAL.SH #####"

restic backup \
  "${EXISTING_SOURCES[@]}" \
  --exclude-file="$RESTIC_EXCLUDE_COMMON" \
  --exclude-file="$RESTIC_EXCLUDE_PERSONAL"

log "##### OFFSITE: RESTIC FORGET/PRUNE #####"
restic forget --keep-last 2 --keep-weekly 8 --keep-monthly 12 --keep-yearly 3 --prune

log "##### [DONE] PERSONAL BACKUP #####"
