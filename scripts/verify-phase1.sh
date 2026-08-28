#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
ENV_FILE="$TESTENV_DIR/backend.env"
[[ -f "$ENV_FILE" ]] || { printf 'Run scripts/testenv-up.sh first\n' >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a
BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
AUTH_HEADER="Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}"

curl -fsS "$BACKEND_URL/healthz" >/dev/null
curl -fsS "$BACKEND_URL/readyz" >/dev/null
overview="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/overview")"
jq -e '.http_only == true and .sites == 5 and .nodes >= 2' <<<"$overview" >/dev/null

for node in codedev nuc; do
  site_slug="north"
  node_cidr="10.32.12.0/24"
  if [[ "$node" == "nuc" ]]; then
    site_slug="east"
    node_cidr="10.32.13.0/24"
  fi
  node_json=""
  for _ in $(seq 1 60); do
    node_json="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/nodes" | jq -e --arg name "$node" '.[] | select(.agent_id == $name)')"
    if jq -e '.applied_version > 0 and .desired_version == .applied_version and .config_status == "in_sync" and .service_status == "healthy" and .liveness_status == "online"' <<<"$node_json" >/dev/null; then
      break
    fi
    sleep 1
  done
  jq -e '.applied_version > 0 and .desired_version == .applied_version and .config_status == "in_sync" and .service_status == "healthy" and .liveness_status == "online"' <<<"$node_json" >/dev/null
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  [[ "$(stat -c '%a' "$state_dir/monitor-state.json")" == "600" ]]
  [[ "$(stat -c '%a' "$state_dir/last-good-bundle.json")" == "600" ]]
  "$ROOT_DIR/singbox/sing-box" check -c "$state_dir/sing-box.json" >/dev/null
  nft -c -f "$state_dir/candidate.nft" >/dev/null
  jq -e '.inbounds | length == 1' "$state_dir/sing-box.json" >/dev/null
  jq -e '.route.rules | length > 0 and any(.[]; has("source_ip_cidr") or .action == "reject")' "$state_dir/sing-box.json" >/dev/null
  jq -e --arg cidr "$node_cidr" 'any(.route.rules[]; (.source_ip_cidr // []) | index($cidr))' "$state_dir/sing-box.json" >/dev/null

  release_id="$(jq -r '.release_id' "$TESTENV_DIR/release-${node}.json")"
  release="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/config/releases/${release_id}")"
  jq -e --arg node "$node" '.status == "succeeded" and .stage == "succeeded" and .progress == 100 and .node_ids == [$node]' <<<"$release" >/dev/null

  draft_id="$(jq -r '.id' "$TESTENV_DIR/draft-${node}.json")"
  node_id="$(<"$TESTENV_DIR/node-${node}.id")"
  site_id="$(<"$TESTENV_DIR/site-${site_slug}.id")"
  retry="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/releases" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -H "Idempotency-Key: phase1-${node}" -d "$(jq -nc --arg draft "$draft_id" --arg site "$site_id" --arg node "$node_id" '{draft_id:$draft,site_id:$site,node_ids:[$node],expected_current_version:null}')")"
  jq -e --arg release "$release_id" '.release_id == $release' <<<"$retry" >/dev/null
done

north_id="$(<"$TESTENV_DIR/site-north.id")"
allowed_payload="$(jq -nc --arg site "$north_id" '{site_id:$site,source_ip:"127.0.0.1"}')"
allowed="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/cidrs/preview" -H 'Content-Type: application/json' -d "$allowed_payload")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$allowed" >/dev/null
denied_payload="$(jq -nc --arg site "$north_id" '{site_id:$site,source_ip:"192.0.2.10"}')"
denied="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/cidrs/preview" -H 'Content-Type: application/json' -d "$denied_payload")"
jq -e '.allowed == false and .reason == "not_in_allowlist"' <<<"$denied" >/dev/null

audit="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/audit/verify")"
jq -e '.valid == true and .event_count > 0' <<<"$audit" >/dev/null

# A node cannot ACK a release generated for a different node, even with a
# syntactically valid request and a newer sequence number.
codedev_release="$(jq -r '.release_id' "$TESTENV_DIR/release-codedev.json")"
cross_node_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BACKEND_URL/agent/v1/ack" -H "Authorization: Bearer $(<"$TESTENV_DIR/nuc.token")" -H 'Content-Type: application/json' -d "$(jq -nc --arg release "$codedev_release" '{node_id:"nuc",release_id:$release,desired_version:999,applied_version:0,bundle_hash:"forged",applied_hash:"",ok:false,sequence:999999}')")"
[[ "$cross_node_status" == "409" ]]

for port in 18080 18081 19090 19091; do
  nc -z 127.0.0.1 "$port"
done
frontend_html="$(curl -fsS "http://127.0.0.1:${GROUPROXY_TEST_FRONTEND_PORT:-3000}")"
rg -q 'grouproxy' <<<"$frontend_html"
printf 'Phase 0/1 validation passed.\n%s\n' "$overview"
