#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required file: $1" >&2
    exit 1
  fi
}

require_cmd docker
require_cmd curl
require_file "$COMPOSE_FILE"
require_file ".env"
require_file "deploy/nginx/default.conf"

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not available" >&2
  exit 1
fi

docker compose -f "$COMPOSE_FILE" ps
curl --fail --silent --show-error http://127.0.0.1/healthz/
echo
docker compose -f "$COMPOSE_FILE" exec web python manage.py check --deploy --fail-level WARNING
