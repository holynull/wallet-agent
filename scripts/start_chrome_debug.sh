#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/start_chrome_debug.sh [url]

Starts a dedicated Chrome profile with remote debugging enabled for DevTools MCP.

Environment overrides:
  CHROME_BIN              Chrome executable path
  CHROME_DEBUG_PORT       DevTools port (default: 9222)
  CHROME_PROFILE_DIR      Profile directory (default: /tmp/wallet-agent-chrome)
  CHROME_START_URL        URL (default: http://localhost:3000)
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "$#" -gt 1 ]]; then
  echo "error: expected at most one URL argument" >&2
  usage >&2
  exit 2
fi

chrome_bin="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
debug_port="${CHROME_DEBUG_PORT:-9222}"
profile_dir="${CHROME_PROFILE_DIR:-/tmp/wallet-agent-chrome}"
start_url="${1:-${CHROME_START_URL:-http://localhost:3000}}"

if [[ ! -x "${chrome_bin}" ]]; then
  echo "error: Chrome executable not found: ${chrome_bin}" >&2
  exit 1
fi

if ! [[ "${debug_port}" =~ ^[0-9]+$ ]] || (( debug_port < 1 || debug_port > 65535 )); then
  echo "error: invalid CHROME_DEBUG_PORT: ${debug_port}" >&2
  exit 2
fi

if curl --noproxy '*' -fsS "http://127.0.0.1:${debug_port}/json/version" >/dev/null 2>&1; then
  echo "Chrome DevTools is already available at http://127.0.0.1:${debug_port}"
  exit 0
fi

mkdir -p "${profile_dir}"
echo "Starting Chrome with DevTools at http://127.0.0.1:${debug_port}"
echo "Profile: ${profile_dir}"
echo "URL: ${start_url}"

exec "${chrome_bin}" \
  --remote-debugging-port="${debug_port}" \
  --user-data-dir="${profile_dir}" \
  "${start_url}"
