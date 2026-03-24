#!/usr/bin/env bash
set -euo pipefail

REF="${1:-origin/main}"
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

require_cmd git
require_cmd docker
require_file "$COMPOSE_FILE"
require_file ".env"

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not available" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Server working tree has local changes; refusing to deploy over a dirty checkout" >&2
  exit 1
fi

git fetch --tags origin

if ! git rev-parse --verify "${REF}^{commit}" >/dev/null 2>&1; then
  echo "Git ref does not resolve to a commit: ${REF}" >&2
  exit 1
fi

git checkout "$REF"
docker compose -f "$COMPOSE_FILE" up -d --build
