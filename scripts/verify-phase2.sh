#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
ENV_FILE="$TESTENV_DIR/backend.env"
[[ -f "$ENV_FILE" ]] || { echo 'Run scripts/testenv-up.sh first' >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a

BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
AUTH_HEADER="Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}"
FIXTURE="$ROOT_DIR/scripts/fixtures/phase2-singbox.json"
INVALID_FIXTURE="$ROOT_DIR/scripts/fixtures/phase2-invalid.txt"
SOURCE_NAME="phase2-fixture"

catalog="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/subscriptions")"
source_id="$(jq -r --arg name "$SOURCE_NAME" '.sources[] | select(.name == $name) | .id' <<<"$catalog" | head -n 1)"
if [[ -z "$source_id" || "$source_id" == "null" ]]; then
  uploaded="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/upload" -H "$AUTH_HEADER" -F "name=$SOURCE_NAME" -F "file=@$FIXTURE")"
  source_id="$(jq -r '.source.id' <<<"$uploaded")"
  version_id="$(jq -r '.version.id' <<<"$uploaded")"
else
  version_id="$(jq -r --arg source "$source_id" '.versions | map(select(.source_id == $source and .parse_ok)) | sort_by(.version) | last | .id' <<<"$catalog")"
fi

[[ -n "$source_id" && "$source_id" != "null" && -n "$version_id" && "$version_id" != "null" ]]
catalog="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/subscriptions")"
jq -e --arg source "$source_id" '(.sources[] | select(.id == $source)) | has("url") | not' <<<"$catalog" >/dev/null
jq -e --arg source "$source_id" '(.sources[] | select(.id == $source)).refreshable == false' <<<"$catalog" >/dev/null
jq -e --arg version "$version_id" '.versions[] | select(.id == $version) | .parse_ok == true and .node_count == 1' <<<"$catalog" >/dev/null
uploaded_refresh_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BACKEND_URL/api/v1/subscriptions/${source_id}/refresh" -H "$AUTH_HEADER")"
[[ "$uploaded_refresh_status" == "409" ]]

north_id="$(<"$TESTENV_DIR/site-north.id")"
east_id="$(<"$TESTENV_DIR/site-east.id")"
expected_hash="$(sha256sum "$FIXTURE" | awk '{print $1}')"
publish_idempotency_key="phase2-publish-${expected_hash}"
publish_payload="$(jq -nc --arg north "$north_id" --arg east "$east_id" '{site_ids:[$north,$east],note:"phase2 integration"}')"
published="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/${source_id}/versions/${version_id}/publish" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -H "Idempotency-Key: ${publish_idempotency_key}" -d "$publish_payload")"
jq -e '.releases | length == 2' <<<"$published" >/dev/null
published_retry="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/${source_id}/versions/${version_id}/publish" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -H "Idempotency-Key: ${publish_idempotency_key}" -d "$publish_payload")"
[[ "$(jq -S '[.releases[] | .release_id]' <<<"$published")" == "$(jq -S '[.releases[] | .release_id]' <<<"$published_retry")" ]]
for node in codedev nuc; do
  release_id="$(jq -r --arg node "$node" '.releases[] | select(.node_ids | index($node)) | .release_id' <<<"$published")"
  [[ -n "$release_id" && "$release_id" != "null" ]]
  release=""
  for _ in $(seq 1 90); do
    release="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/config/releases/${release_id}")"
    if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release" >/dev/null; then
      break
    fi
    sleep 1
  done
  jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release" >/dev/null
  node_json=""
  for _ in $(seq 1 15); do
    node_json="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/nodes" | jq -e --arg node "$node" '.[] | select(.agent_id == $node)')"
    if jq -e '.subscription_status == "current" and .config_status == "in_sync"' <<<"$node_json" >/dev/null; then
      break
    fi
    sleep 1
  done
  jq -e '.subscription_status == "current" and .config_status == "in_sync"' <<<"$node_json" >/dev/null
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  jq -e --arg hash "$expected_hash" '.subscription.hash == $hash and .subscription.format == "sing-box"' "$state_dir/last-good-bundle.json" >/dev/null
  jq -e '.route.final == "subscription" and any(.outbounds[]; .tag == "subscription" and .type == "selector")' "$state_dir/sing-box.json" >/dev/null
  jq -e 'any(.outbounds[]; .tag == "phase2-edge-a" and .type == "shadowsocks")' "$state_dir/sing-box.json" >/dev/null
  jq -e '(.route.rule_set | length == 2) and all(.route.rule_set[]; .type == "local" and .format == "binary" and (.tag == "geoip-cn" or .tag == "geosite-cn"))' "$state_dir/sing-box.json" >/dev/null
  jq -e 'any(.route.rules[]; .outbound == "direct" and .rule_set == ["geoip-cn", "geosite-cn"])' "$state_dir/sing-box.json" >/dev/null
  while IFS= read -r rule_set_path; do
    [[ -f "$rule_set_path" ]]
  done < <(jq -r '.route.rule_set[].path' "$state_dir/sing-box.json")
  "$ROOT_DIR/singbox/sing-box" check -c "$state_dir/sing-box.json" >/dev/null
done

# The test environment lowers the inline threshold, so this fixture exercises
# the token-protected blob path instead of only the inline bundle path.
actual_blob_hash="$(curl -fsS -H "Authorization: Bearer $(<"$TESTENV_DIR/codedev.token")" "$BACKEND_URL/agent/v1/blobs/${expected_hash}" | sha256sum | awk '{print $1}')"
[[ "$actual_blob_hash" == "$expected_hash" ]]

# A source has one active refresh task regardless of repeated manual requests.
# The loopback URL is never fetched because backend SSRF checks reject it;
# it only keeps the queued task active long enough to exercise task merging.
merge_name="phase2-refresh-merge"
merge_catalog="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/subscriptions")"
merge_source="$(jq -r --arg name "$merge_name" '.sources[] | select(.name == $name) | .id' <<<"$merge_catalog" | head -n 1)"
if [[ -z "$merge_source" || "$merge_source" == "null" ]]; then
  merge_created="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$(jq -nc --arg name "$merge_name" '{name:$name,url:"http://127.0.0.1/subscription",fetch_interval_sec:21600}')")"
  merge_source="$(jq -r '.source.id' <<<"$merge_created")"
  merge_task="$(jq -r '.task.task_id' <<<"$merge_created")"
else
  merge_first="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/${merge_source}/refresh" -H "$AUTH_HEADER")"
  merge_task="$(jq -r '.task.task_id' <<<"$merge_first")"
fi
merge_second="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/${merge_source}/refresh" -H "$AUTH_HEADER")"
[[ "$merge_task" == "$(jq -r '.task.task_id' <<<"$merge_second")" ]]

invalid_name="phase2-invalid"
invalid_catalog="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/subscriptions")"
invalid_source="$(jq -r --arg name "$invalid_name" '.sources[] | select(.name == $name) | .id' <<<"$invalid_catalog" | head -n 1)"
if [[ -z "$invalid_source" || "$invalid_source" == "null" ]]; then
  invalid_upload="$(curl -fsS -X POST "$BACKEND_URL/api/v1/subscriptions/upload" -H "$AUTH_HEADER" -F "name=$invalid_name" -F "file=@$INVALID_FIXTURE")"
  invalid_source="$(jq -r '.source.id' <<<"$invalid_upload")"
  invalid_version="$(jq -r '.version.id' <<<"$invalid_upload")"
else
  invalid_version="$(jq -r --arg source "$invalid_source" '.versions | map(select(.source_id == $source)) | sort_by(.version) | last | .id' <<<"$invalid_catalog")"
fi
jq -e --arg source "$invalid_source" '.sources[] | select(.id == $source) | .last_refresh_error != ""' <<<"$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/subscriptions")" >/dev/null
publish_invalid_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BACKEND_URL/api/v1/subscriptions/${invalid_source}/versions/${invalid_version}/publish" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$(jq -nc --arg north "$north_id" '{site_ids:[$north]}')")"
[[ "$publish_invalid_status" == "409" ]]

echo "Phase 2 validation passed. Subscription hash: $expected_hash"
