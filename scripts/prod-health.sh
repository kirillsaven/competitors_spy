#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"

load_env() {
  set -a
  . ./.env
  set +a
}

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

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env var: $name" >&2
    exit 1
  fi
}

require_cmd docker
require_cmd curl
require_file "$COMPOSE_FILE"
require_file ".env"
require_file "deploy/nginx/default.conf.template"

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not available" >&2
  exit 1
fi

load_env
require_env PROXY_SERVER_NAME
require_env PROXY_TLS_CERT_PATH
require_env PROXY_TLS_KEY_PATH
require_file "$PROXY_TLS_CERT_PATH"
require_file "$PROXY_TLS_KEY_PATH"

docker compose -f "$COMPOSE_FILE" ps
curl --fail --silent --show-error --resolve "${PROXY_SERVER_NAME}:443:127.0.0.1" "https://${PROXY_SERVER_NAME}/healthz/"
echo
docker compose -f "$COMPOSE_FILE" exec web python manage.py check --deploy --fail-level WARNING
