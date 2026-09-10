#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

usage() {
  cat <<'EOF'
Usage: ./scripts/start_local.sh [--reload]

Starts the local Wallet Agent FastAPI service.

Environment overrides:
  WALLET_AGENT_HOST       Bind host (default: 127.0.0.1)
  WALLET_AGENT_PORT       Bind port (default: 8000)
  WALLET_AGENT_LOG_LEVEL  Uvicorn log level (default: info)
  WALLET_AGENT_UVICORN    Path to the uvicorn executable
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

reload_args=()
if [[ "${1:-}" == "--reload" ]]; then
  reload_args=(--reload)
  shift
fi

if [[ "$#" -gt 0 ]]; then
  echo "error: unknown argument: $1" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
  echo "error: ${PROJECT_ROOT}/.env is missing" >&2
  echo "copy .env.example to .env and fill in the required configuration" >&2
  exit 1
fi

uvicorn_bin="${WALLET_AGENT_UVICORN:-${PROJECT_ROOT}/.venv/bin/uvicorn}"
if [[ ! -x "${uvicorn_bin}" ]]; then
  if command -v uvicorn >/dev/null 2>&1; then
    uvicorn_bin="$(command -v uvicorn)"
  else
    echo "error: uvicorn was not found; run 'uv sync' first" >&2
    exit 1
  fi
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${PROJECT_ROOT}/src:${PYTHONPATH}"
else
  export PYTHONPATH="${PROJECT_ROOT}/src"
fi

host="${WALLET_AGENT_HOST:-127.0.0.1}"
port="${WALLET_AGENT_PORT:-8000}"
log_level="${WALLET_AGENT_LOG_LEVEL:-info}"

echo "Starting Wallet Agent at http://${host}:${port}"
echo "Demo: http://localhost:${port}/demo/"

uvicorn_args=(
  wallet_agent.main:app
  --host "${host}"
  --port "${port}"
  --log-level "${log_level}"
)
if [[ "${#reload_args[@]}" -gt 0 ]]; then
  uvicorn_args+=("${reload_args[@]}")
fi

exec "${uvicorn_bin}" "${uvicorn_args[@]}"
