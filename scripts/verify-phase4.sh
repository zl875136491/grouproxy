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

[[ -n "${GROUPROXY_PROXY_CREDENTIAL_SECRET:-}" ]] || {
  printf 'The test backend needs GROUPROXY_PROXY_CREDENTIAL_SECRET. Restart it with scripts/testenv-up.sh.\n' >&2
  exit 1
}

BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
AUTH_HEADER="Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}"
north_id="$(<"$TESTENV_DIR/site-north.id")"
node_id="$(<"$TESTENV_DIR/node-codedev.id")"
body_file="$(mktemp "$TESTENV_DIR/.phase4-credential.XXXXXX")"
headers_file="$(mktemp "$TESTENV_DIR/.phase4-headers.XXXXXX")"
trap 'rm -f "$body_file" "$headers_file"' EXIT
chmod 600 "$body_file" "$headers_file"

# Make the flow repeatable: prepare the credential while the listener remains
# network-only, then enable Basic authentication through a normal release.
curl -fsS -X PUT "$BACKEND_URL/api/v1/sites/${north_id}/proxy-auth" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -d '{"required":false}' >/dev/null
curl -fsS -D "$headers_file" -o "$body_file" \
  -X POST "$BACKEND_URL/api/v1/access/proxy-credentials/${north_id}/rotate" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d '{}' >/dev/null
grep -qi '^Cache-Control: no-store' "$headers_file"
jq -e '.username != "" and .password != "" and (.password | length >= 32)' "$body_file" >/dev/null
username="$(jq -r '.username' "$body_file")"
password="$(jq -r '.password' "$body_file")"

curl -fsS -X PUT "$BACKEND_URL/api/v1/sites/${north_id}/proxy-auth" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -d '{"required":true}' >/dev/null
draft="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/drafts" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg site "$north_id" --arg node "$node_id" \
    '{site_id:$site,node_ids:[$node],diff:{proxy_auth:{required:true}},note:"phase4 proxy authentication validation"}')")"
draft_id="$(jq -r '.id' <<<"$draft")"
release="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/releases" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: phase4-proxy-auth-$(date +%s%N)" \
  -d "$(jq -nc --arg draft "$draft_id" --arg site "$north_id" --arg node "$node_id" \
    '{draft_id:$draft,site_id:$site,node_ids:[$node],expected_current_version:null}')")"
release_id="$(jq -r '.release_id' <<<"$release")"

for _ in $(seq 1 60); do
  release_state="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/config/releases/${release_id}")"
  if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release_state" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release_state" >/dev/null

config_file="$TESTENV_DIR/monitor-codedev/state/sing-box.json"
"$ROOT_DIR/singbox/sing-box" check -c "$config_file" >/dev/null
jq -e --arg username "$username" '
  .inbounds[]
  | select(.type == "http")
  | (.users // [])
  | any(.[]; .username == $username and (.password | length >= 32))
' "$config_file" >/dev/null

access_config="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/access/config")"
proxy_host="$(jq -r '.fqdn' <<<"$access_config")"
proxy_port="$(jq -r '.port' <<<"$access_config")"
[[ "$proxy_port" == "80" ]]
# `--resolve` is curl's request-scoped hosts override, so no system resolver
# state changes. The request exercises the same public :80 Nginx entrypoint
# used by employee clients, not sing-box's loopback-only internal listener.
test_proxy_port="${GROUPROXY_TEST_PUBLIC_PORT:-80}"
proxy_url="http://${proxy_host}:${test_proxy_port}"
proxy_resolve="${proxy_host}:${test_proxy_port}:127.0.0.1"

# A missing Basic credential must be rejected locally before any destination
# traffic is opened. The optional external check below makes only one HEAD
# request to the user-approved Google URL.
unauthorized_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
  --noproxy '' --proxy "$proxy_url" --resolve "$proxy_resolve" \
  http://www.google.com/ncr || true)"
[[ "$unauthorized_status" == "407" ]]

if [[ "${GROUPROXY_VERIFY_PROXY_EXTERNAL:-0}" == "1" ]]; then
  curl -fsS --head --max-time 8 --connect-timeout 4 --noproxy '' \
    --proxy "$proxy_url" --resolve "$proxy_resolve" \
    --proxy-user "${username}:${password}" https://www.google.com/ncr >/dev/null
fi

backup_response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/backups" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: phase4-backup-rehearsal-$(date +%s%N)" \
  -d '{"scope":"control_plane"}')"
backup_id="$(jq -r '.backup.backup_id' <<<"$backup_response")"
backup_task_id="$(jq -r '.task.task_id' <<<"$backup_response")"
[[ -n "$backup_id" && "$backup_id" != "null" && -n "$backup_task_id" && "$backup_task_id" != "null" ]]

backup_task=""
for _ in $(seq 1 90); do
  backup_task="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/tasks/${backup_task_id}")"
  if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$backup_task" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100 and .result.collections > 0' <<<"$backup_task" >/dev/null

backup_record="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/backups" | jq -e --arg backup "$backup_id" '.[] | select(.backup_id == $backup)')"
jq -e '.origin == "manual" and .status == "verified" and .encrypted == true and (.storage_ref | length > 0) and .size_bytes > 0 and .verified_at != null and .last_rehearsed_at == null' <<<"$backup_record" >/dev/null
backup_artifact="$(jq -r '.storage_ref' <<<"$backup_record")"
[[ -f "$TESTENV_DIR/backups/${backup_artifact}" ]]
[[ "$(stat -c '%a' "$TESTENV_DIR/backups/${backup_artifact}")" == "600" ]]

rehearsal_response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/backups/${backup_id}/restore" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: phase4-backup-rehearsal-check-$(date +%s%N)" \
  -d '{"confirm":false}')"
rehearsal_task_id="$(jq -r '.task.task_id' <<<"$rehearsal_response")"
[[ -n "$rehearsal_task_id" && "$rehearsal_task_id" != "null" ]]

rehearsal_task=""
for _ in $(seq 1 90); do
  rehearsal_task="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/tasks/${rehearsal_task_id}")"
  if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$rehearsal_task" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e '.status == "succeeded" and .result.mode == "rehearsal" and .result.applied == 0 and .result.collections > 0' <<<"$rehearsal_task" >/dev/null

backup_record="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/backups" | jq -e --arg backup "$backup_id" '.[] | select(.backup_id == $backup)')"
jq -e '.origin == "manual" and .status == "rehearsed" and .verified_at != null and .last_rehearsed_at != null and .encrypted == true' <<<"$backup_record" >/dev/null

printf 'Phase 4 HTTP Basic authentication and encrypted backup rehearsal validation passed.\n'
