#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo '##### PULLING NEW IMAGES #####'
docker compose -f "$STACK_DIR/docker-compose.yml" pull

echo '##### RESTARTING CONTAINERS #####'
docker compose -f "$STACK_DIR/docker-compose.yml" up -d --remove-orphans

bash "$SCRIPT_DIR/cleanup.sh"
