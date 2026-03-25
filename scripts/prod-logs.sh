#!/usr/bin/env bash
set -euo pipefail

TAIL_LINES="${1:-100}"
COMPOSE_FILE="docker-compose.prod.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "Missing required command: docker" >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Missing required file: $COMPOSE_FILE" >&2
  exit 1
fi

if [[ ! -f "deploy/nginx/default.conf.template" ]]; then
  echo "Missing required file: deploy/nginx/default.conf.template" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not available" >&2
  exit 1
fi

docker compose -f "$COMPOSE_FILE" logs --tail="$TAIL_LINES" proxy web bot worker beat
