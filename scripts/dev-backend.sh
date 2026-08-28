#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${GROUPROXY_ENV_FILE:-$ROOT_DIR/testenv/backend.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
elif [[ -z "${GROUPROXY_BUNDLE_HMAC_SECRET:-}" || -z "${GROUPROXY_ADMIN_PASSWORD:-}" || -z "${GROUPROXY_MANAGEMENT_TOKEN:-}" ]]; then
  printf 'Missing %s and required GROUPROXY_* credentials are not set. Run scripts/testenv-up.sh first.\n' "$ENV_FILE" >&2
  exit 1
fi
cd "$ROOT_DIR/backend"
exec "$ROOT_DIR/.venv/bin/uvicorn" main:app --host "${GROUPROXY_HOST:-0.0.0.0}" --port "${GROUPROXY_PORT:-8000}"
