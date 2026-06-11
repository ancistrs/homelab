#!/bin/bash
set -e

echo '##### START CLEANUP #####'
docker container prune -f
docker image prune -a -f
docker volume prune -f
docker network prune -f
