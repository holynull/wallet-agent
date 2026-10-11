#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-120}"
NO_CACHE=0
PROXY_URL=""

usage() {
  cat <<'EOF'
Usage: scripts/redeploy.sh [options]

Rebuild and start the local Docker Compose deployment without deleting data volumes.

Options:
  --no-cache             Rebuild images without Docker build cache.
  --proxy URL            Set HTTP(S)_PROXY for image and dependency downloads.
  --timeout SECONDS      Health-check timeout (default: 120).
  -h, --help             Show this help.

Examples:
  scripts/redeploy.sh
  scripts/redeploy.sh --no-cache
  scripts/redeploy.sh --proxy http://host.docker.internal:7890
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while (($# > 0)); do
  case "$1" in
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --proxy)
      (($# >= 2)) || die "--proxy requires a URL"
      PROXY_URL="$2"
      shift 2
      ;;
    --timeout)
      (($# >= 2)) || die "--timeout requires seconds"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || die "timeout must be a non-negative integer"
command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
[[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"

cd "$REPO_ROOT"

if [[ -n "$PROXY_URL" ]]; then
  export HTTP_PROXY="$PROXY_URL"
  export HTTPS_PROXY="$PROXY_URL"
  export http_proxy="$PROXY_URL"
  export https_proxy="$PROXY_URL"
  export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,mysql,postgres,redis}"
  export no_proxy="${no_proxy:-$NO_PROXY}"
  printf 'Using build proxy: %s\n' "$PROXY_URL"
fi

compose=(docker compose -f "$COMPOSE_FILE")
if ((NO_CACHE)); then
  printf 'Building images without cache...\n'
  "${compose[@]}" build --no-cache wallet-agent demo-next
  printf 'Starting services...\n'
  "${compose[@]}" up -d --force-recreate
else
  printf 'Building and starting services...\n'
  "${compose[@]}" up -d --build
fi

wait_for_url() {
  local name="$1"
  local url="$2"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if curl --noproxy '*' --fail --silent --show-error "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$name" "$url"
      return 0
    fi
    sleep 2
  done
  printf '%s did not become ready: %s\n' "$name" "$url" >&2
  return 1
}

if ! wait_for_url "API health" "http://127.0.0.1:8000/health" \
  || ! wait_for_url "API readiness" "http://127.0.0.1:8000/ready" \
  || ! wait_for_url "Demo" "http://127.0.0.1:3000/"; then
  "${compose[@]}" ps
  printf '\nRecent service logs:\n' >&2
  "${compose[@]}" logs --tail=80 wallet-agent demo-next >&2 || true
  exit 1
fi

"${compose[@]}" ps
printf 'Redeploy complete. API: http://localhost:8000  Demo: http://localhost:3000\n'
