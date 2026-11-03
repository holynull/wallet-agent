#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/keychain_secret.sh store  <service>
  scripts/keychain_secret.sh check  <service>
  scripts/keychain_secret.sh delete <service>
  scripts/keychain_secret.sh exec   <service> <ENV_NAME> -- <command> [args...]

Examples:
  scripts/keychain_secret.sh store  catwallet-admin-token-prod
  scripts/keychain_secret.sh check  catwallet-admin-token-prod
  scripts/keychain_secret.sh exec   catwallet-admin-token-prod ADMIN_TOKEN -- \
    curl http://127.0.0.1:8000/debug/requests
  scripts/keychain_secret.sh delete catwallet-admin-token-prod
EOF
}

command_name=${1:-}
service=${2:-}

if [[ -z "$command_name" || -z "$service" ]]; then
  usage
  exit 2
fi

read_secret() {
  if ! security find-generic-password -s "$service" -w >/dev/null 2>&1; then
    echo "Keychain item not found: $service" >&2
    exit 1
  fi
  security find-generic-password -s "$service" -w
}

case "$command_name" in
  store)
    umask 077
    printf '输入 %s 的 secret: ' "$service"
    IFS= read -rs secret
    printf '\n'
    if [[ -z "$secret" ]]; then
      echo "Secret must not be empty." >&2
      exit 1
    fi
    security add-generic-password \
      -U \
      -a "$USER" \
      -s "$service" \
      -w "$secret" >/dev/null
    stored=$(read_secret)
    if [[ "$stored" == "$secret" ]]; then
      echo "Stored and verified: $service (${#stored} characters)"
    else
      echo "Verification failed: $service" >&2
      unset secret stored
      exit 1
    fi
    unset secret stored
    ;;
  check)
    value=$(read_secret)
    echo "OK: $service (${#value} characters)"
    unset value
    ;;
  delete)
    if security delete-generic-password -s "$service" >/dev/null 2>&1; then
      echo "Deleted: $service"
    else
      echo "Keychain item not found: $service" >&2
      exit 1
    fi
    ;;
  exec)
    env_name=${3:-}
    if [[ -z "$env_name" || "${4:-}" != "--" ]]; then
      usage
      exit 2
    fi
    shift 4
    if [[ $# -eq 0 ]]; then
      usage
      exit 2
    fi
    value=$(read_secret)
    export "$env_name=$value"
    unset value
    "$@"
    ;;
  *)
    usage
    exit 2
    ;;
esac
