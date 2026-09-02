#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
ENV_FILE="$TESTENV_DIR/backend.env"

[[ -f "$ENV_FILE" ]] || {
  printf 'Run scripts/testenv-up.sh first.\n' >&2
  exit 1
}

set -a
source "$ENV_FILE"
set +a

BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
FRONTEND_URL="http://127.0.0.1:${GROUPROXY_TEST_FRONTEND_PORT:-3000}"
DASHBOARD_BASE_PATH="${GROUPROXY_TEST_DASHBOARD_BASE_PATH:-/dashboard}"
AUTH_HEADER="Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}"

curl -fsS "$BACKEND_URL/healthz" >/dev/null
curl -fsS "$BACKEND_URL/readyz" >/dev/null

overview="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/overview")"
jq -e '
  .http_only == true and
  (.connections | type) == "number" and
  (.open_circuits | type) == "number" and
  (.open_alerts | type) == "number"
' <<<"$overview" >/dev/null

logs="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/logs?limit=20")"
jq -e '
  type == "array" and
  all(.[]; (.ts | type) == "string" and (.bytes_up | type) == "number" and
    (.bytes_down | type) == "number" and (.duration_ms | type) == "number")
' <<<"$logs" >/dev/null

connections="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/connections?limit=20")"
jq -e '
  type == "array" and
  all(.[]; (.sampled_at | type) == "string" and
    (.active_connections | type) == "number" and (.bytes_up | type) == "number" and
    (.bytes_down | type) == "number" and (.api_available | type) == "boolean")
' <<<"$connections" >/dev/null

alerts="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/alerts")"
jq -e '
  type == "array" and
  all(.[]; (.severity | type) == "string" and (.status | type) == "string" and
    (.first_seen_at | type) == "string" and (.last_seen_at | type) == "string")
' <<<"$alerts" >/dev/null

for node in codedev nuc; do
  node_id="$(<"$TESTENV_DIR/node-${node}.id")"
  probes="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/nodes/${node_id}/probes?limit=20")"
  jq -e '
    (.history | type) == "array" and (.circuits | type) == "array" and
    all(.history[]; (.latency_ms | type) == "number" and (.sampled_at | type) == "string") and
    all(.circuits[]; (.last_latency_ms | type) == "number" and (.state | type) == "string")
  ' <<<"$probes" >/dev/null
done

audit="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/audit/verify")"
jq -e '.valid == true and (.event_count | type) == "number"' <<<"$audit" >/dev/null
curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/audit/export?export_format=ndjson" \
  | jq -R -s 'split("\n") | map(select(length > 0) | fromjson) | type == "array"' \
  | grep -qx true

access_config="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/access/config")"
jq -e '.protocol == "http-connect" and .https_proxy_enabled == false and (.port | type) == "number"' \
  <<<"$access_config" >/dev/null
curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/access/proxy.pac" \
  | grep -q 'return "PROXY '
linux_setup_script="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/access/linux-setup.sh")"
grep -q 'HTTPS transport is intentionally disabled.' <<<"$linux_setup_script"
grep -q -- '--uninstall' <<<"$linux_setup_script"
grep -q 'gsettings set org.gnome.system.proxy mode manual' <<<"$linux_setup_script"
grep -q 'kwriteconfig' <<<"$linux_setup_script"

curl -fsS "$FRONTEND_URL${DASHBOARD_BASE_PATH}/alerts" | grep -qi 'grouproxy'
printf 'Phase 3 local observability and access validation passed.\n'
