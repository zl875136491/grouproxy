#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/frontend"
if [[ ! -d node_modules ]]; then npm install; fi
exec env NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next-dev}" npm run dev -- --hostname "${GROUPROXY_FRONTEND_HOST:-0.0.0.0}" --port "${GROUPROXY_FRONTEND_PORT:-3000}"
