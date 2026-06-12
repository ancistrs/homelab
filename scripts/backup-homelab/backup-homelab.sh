#!/bin/bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$STACK_DIR/.env"
BACKUP_DIR="$STACK_DIR/backups"
POSTGRES_CONTAINER="postgres"
POSTGRES_HOST="postgres"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
ROTATE_KEEP=1
FAILED=0

log(){ printf '[%s] [homelab] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
up(){ printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

mkdir -p \
  "$BACKUP_DIR/uptimekuma" \
  "$BACKUP_DIR/n8n" \
  "$BACKUP_DIR/paperless" \
  "$BACKUP_DIR/grafana" \
  "$BACKUP_DIR/nocodb" \
  "$BACKUP_DIR/whisper" \
  "$BACKUP_DIR/tailscale" \
  "$BACKUP_DIR/scripts" \
  "$BACKUP_DIR/postgres" \
  "$BACKUP_DIR/meta"

trap 'log "ERROR on line $LINENO"; exit 1' ERR

set -a
. "$ENV_FILE"
set +a
: "${POSTGRES_USER:=admin}"
: "${POSTGRES_ADMIN_PASSWORD:?POSTGRES_ADMIN_PASSWORD missing in $ENV_FILE}"

if command -v gtar >/dev/null 2>&1; then TAR=gtar; else TAR=tar; fi
if command -v zstd >/dev/null 2>&1; then HAS_ZSTD=1; EXT="tar.zst"; else HAS_ZSTD=0; EXT="tar.gz"; fi

keep_n(){
  local pattern="$1"
  ls -1t $pattern 2>/dev/null | awk -v N="$ROTATE_KEEP" 'NR>N' | while IFS= read -r f; do
    [ -n "$f" ] && rm -f -- "$f"
  done || true
}

tar_dir(){
  local src="$1" base="$2"; shift 2
  local outdir="$BACKUP_DIR/$base"
  local out="$outdir/${base##*/}_${RUN_ID}.$EXT"
  mkdir -p "$outdir"

  if [ ! -d "$src" ]; then
    log "##### $(up "$base") SKIP (source dir missing: $src) #####"
    return 0
  fi

  local -a excl_args=(--exclude=.DS_Store --exclude=.Trashes --exclude=.Spotlight-V100 --exclude=.fseventsd)
  for ex in "$@"; do excl_args+=("--exclude=$ex"); done

  log "##### $(up "$base") START #####"
  if [ "$HAS_ZSTD" -eq 1 ]; then
    ( cd "$src" && $TAR -cf - "${excl_args[@]+"${excl_args[@]}"}" . ) | zstd -T0 -19 -o "$out"
  else
    ( cd "$src" && $TAR -cf - "${excl_args[@]+"${excl_args[@]}"}" . ) | gzip > "$out"
  fi

  # verify archive is not empty / corrupt
  local size
  size=$(stat -f%z "$out" 2>/dev/null || stat -c%s "$out" 2>/dev/null || echo 0)
  if [ "$size" -lt 100 ]; then
    log "##### $(up "$base") ERROR: archive too small (${size}B) #####"
    return 1
  fi

  keep_n "$outdir/${base##*/}_*.$EXT"
  log "##### $(up "$base") SUCCESS ($(( size / 1024 / 1024 ))MB) #####"
}

log "##### [START] BACKUP-HOMELAB.SH #####"

# --- META SNAPSHOTS ---
log "##### META SNAPSHOTS START #####"
install -m 0600 "$STACK_DIR/docker-compose.yml" "$BACKUP_DIR/meta/docker-compose_${RUN_ID}.yml"
keep_n "$BACKUP_DIR/meta/docker-compose_*.yml"
install -m 0600 "$ENV_FILE" "$BACKUP_DIR/meta/env_${RUN_ID}.bak"
keep_n "$BACKUP_DIR/meta/env_*.bak"
install -m 0600 "$STACK_DIR/CLAUDE.md" "$BACKUP_DIR/meta/CLAUDE_${RUN_ID}.md"
keep_n "$BACKUP_DIR/meta/CLAUDE_*.md"
install -m 0600 "$STACK_DIR/.gitignore" "$BACKUP_DIR/meta/gitignore_${RUN_ID}.bak"
keep_n "$BACKUP_DIR/meta/gitignore_*.bak"
install -m 0600 "$STACK_DIR/.env.example" "$BACKUP_DIR/meta/env-example_${RUN_ID}.bak"
keep_n "$BACKUP_DIR/meta/env-example_*.bak"
log "##### META SNAPSHOTS END #####"

# --- SERVICE DATA ARCHIVES ---
tar_dir "$STACK_DIR/uptimekuma"          "uptimekuma"
tar_dir "$STACK_DIR/n8n"                 "n8n"

log "##### [EXTRA] PAPERLESS DOCUMENT RENAMER START #####"
docker exec -i paperless python3 manage.py document_renamer
log "##### [EXTRA] PAPERLESS DOCUMENT RENAMER SUCCESS #####"

tar_dir "$STACK_DIR/paperless"           "paperless" "*/thumbnails/*"
tar_dir "$STACK_DIR/grafana"             "grafana"
tar_dir "$STACK_DIR/nocodb"              "nocodb"
tar_dir "$STACK_DIR/whisper"             "whisper" "__pycache__" "*.log" "*.err"
tar_dir "$STACK_DIR/tailscale"           "tailscale"
tar_dir "$STACK_DIR/scripts"             "scripts" "*.log"

# --- POSTGRES DUMPS ---
dump_db(){
  local db="$1"
  local tmp="/var/lib/postgresql/${db}_${RUN_ID}.dump"
  local out="$BACKUP_DIR/postgres/pg_${db}_${RUN_ID}.dump"
  log "##### PG:$db START #####"
  if docker exec -e PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" "$POSTGRES_CONTAINER" \
      pg_dump -Fc -U "$POSTGRES_USER" -h "$POSTGRES_HOST" -p 5432 -d "$db" -f "$tmp"; then
    docker cp "${POSTGRES_CONTAINER}:${tmp}" "$out"
    docker exec "$POSTGRES_CONTAINER" rm -f "$tmp"
    keep_n "$BACKUP_DIR/postgres/pg_${db}_*.dump"
    log "##### PG:$db SUCCESS -> $out #####"
  else
    log "##### PG:$db FAILED #####"
    FAILED=1
  fi
}

log "##### PG:GLOBALS START #####"
docker exec \
  -e PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" \
  -e PGUSER="$POSTGRES_USER" \
  "$POSTGRES_CONTAINER" \
  sh -c "pg_dumpall --username=$POSTGRES_USER --globals-only" \
  > "$BACKUP_DIR/postgres/pg_globals_${RUN_ID}.sql"
keep_n "$BACKUP_DIR/postgres/pg_globals_*.sql"
log "##### PG:GLOBALS SUCCESS #####"

# dump ALL non-template databases (auto-discovers new ones)
DBS=$(docker exec -e PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" "$POSTGRES_CONTAINER" \
  psql -U "$POSTGRES_USER" -h "$POSTGRES_HOST" -p 5432 --dbname=postgres -At \
  -c "SELECT datname FROM pg_database WHERE datistemplate=false;")
for db in $DBS; do dump_db "$db"; done

# --- OFFSITE: RESTIC ---
RESTIC_ENV="$HOME/.restic/homelab.env"
RESTIC_EXCLUDE_COMMON="$HOME/.restic/excludes/common.txt"
RESTIC_EXCLUDE_HOMELAB="$HOME/.restic/excludes/homelab.txt"
[ -f "$RESTIC_ENV" ] || { log "Restic env not found: $RESTIC_ENV"; exit 1; }
[ -f "$RESTIC_EXCLUDE_COMMON" ] || { log "Restic exclude file not found: $RESTIC_EXCLUDE_COMMON"; exit 1; }
[ -f "$RESTIC_EXCLUDE_HOMELAB" ] || { log "Restic exclude file not found: $RESTIC_EXCLUDE_HOMELAB"; exit 1; }
command -v restic >/dev/null 2>&1 || { log "restic not installed"; exit 1; }
. "$RESTIC_ENV"

log "##### OFFSITE: RESTIC BACKUP START #####"
# back up source directories directly for proper deduplication,
# plus pg dumps which must come from the backup dir
restic backup \
  "$STACK_DIR/uptimekuma" \
  "$STACK_DIR/n8n" \
  "$STACK_DIR/paperless" \
  "$STACK_DIR/grafana" \
  "$STACK_DIR/nocodb" \
  "$STACK_DIR/whisper" \
  "$STACK_DIR/tailscale" \
  "$STACK_DIR/scripts" \
  "$STACK_DIR/docker-compose.yml" \
  "$STACK_DIR/CLAUDE.md" \
  "$STACK_DIR/.gitignore" \
  "$STACK_DIR/.env.example" \
  "$ENV_FILE" \
  "$BACKUP_DIR/postgres" \
  --exclude-file="$RESTIC_EXCLUDE_COMMON" \
  --exclude-file="$RESTIC_EXCLUDE_HOMELAB"

log "##### OFFSITE: RESTIC FORGET/PRUNE #####"
restic forget --keep-last 2 --keep-weekly 4 --keep-monthly 6 --prune
log "##### OFFSITE: RESTIC BACKUP SUCCESS #####"

if [ "$FAILED" -ne 0 ]; then
  log "##### [DONE WITH ERRORS] CHECK PG DUMP FAILURES ABOVE #####"
  exit 1
fi

log "##### [DONE] BACKUPS IN $BACKUP_DIR #####"
log "##### -------------------------------- #####"
