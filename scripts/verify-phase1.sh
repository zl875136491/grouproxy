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
TEST_CSRF_ORIGIN="${GROUPROXY_TEST_CSRF_ORIGIN:-http://${GROUPROXY_PROXY_ACCESS_FQDN:-test-proxy.1oa.com.cn}:${GROUPROXY_TEST_FRONTEND_PORT:-3000}}"
curl() { command curl -H "Origin: ${TEST_CSRF_ORIGIN}" "$@"; }

wait_for_node_in_sync() {
  local node="$1" node_json=""
  for _ in $(seq 1 90); do
    node_json="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/nodes" | jq -e --arg name "$node" '.[] | select(.agent_id == $name)')"
    if jq -e '.applied_version > 0 and .desired_version == .applied_version and .config_status == "in_sync" and .service_status == "healthy" and .liveness_status == "online"' <<<"$node_json" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  printf 'Node %s did not reach an in-sync healthy state.\n' "$node" >&2
  return 1
}

wait_for_source_blacklist_mutation() {
  local mutation="$1" release_id release node
  local -a release_ids node_ids
  jq -e '
    (.rule.id | type == "string") and
    (.distribution | type == "array") and
    all(.distribution[]; .state == "released" or .state == "no_nodes" or .state == "no_effect") and
    all(.distribution[] | select(.state == "released"); (.release.release_id | type == "string"))
  ' <<<"$mutation" >/dev/null
  mapfile -t release_ids < <(jq -r '.distribution[] | select(.state == "released") | .release.release_id' <<<"$mutation")
  for release_id in "${release_ids[@]}"; do
    for _ in $(seq 1 90); do
      release="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/config/releases/${release_id}")"
      if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release" >/dev/null; then
        break
      fi
      sleep 1
    done
    if ! jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$release" >/dev/null; then
      printf 'Automatic blacklist release %s did not complete.\n' "$release_id" >&2
      return 1
    fi
  done
  mapfile -t node_ids < <(jq -r '.distribution[] | select(.state == "released") | .node_ids[]?' <<<"$mutation" | sort -u)
  for node in "${node_ids[@]}"; do
    wait_for_node_in_sync "$node"
  done
}

preview_source() {
  local node_id="$1" source_ip="$2"
  curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg node "$node_id" --arg source "$source_ip" '{node_id:$node,source_ip:$source}')"
}

preview_dest() {
  local node_id="$1" dest_host="$2"
  curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg node "$node_id" --arg dest "$dest_host" '{node_id:$node,dest_host:$dest}')"
}

PHASE1_EXCLUDED_SOURCES=""
find_allowed_source() {
  local prefix host candidate node_id preview allowed
  for prefix in 192.0.2 198.51.100 203.0.113 198.18.240; do
    for host in $(seq 1 254); do
      candidate="${prefix}.${host}"
      [[ ",${PHASE1_EXCLUDED_SOURCES}," == *",${candidate},"* ]] && continue
      allowed=1
      for node_id in "$@"; do
        preview="$(preview_source "$node_id" "$candidate")"
        if ! jq -e '.allowed == true and .reason == "allowed"' <<<"$preview" >/dev/null; then
          allowed=0
          break
        fi
      done
      if [[ "$allowed" == "1" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  done
  printf 'Could not find an address allowed by every requested test node.\n' >&2
  return 1
}

json_node_ids() {
  printf '%s\n' "$@" | jq -R . | jq -s .
}

create_blacklist() {
  local kind="$1" direction="$2" pattern="$3" comment="$4"
  shift 4
  local payload response status body
  payload="$(jq -nc --argjson nodes "$(json_node_ids "$@")" --arg kind "$kind" --arg direction "$direction" --arg pattern "$pattern" --arg comment "$comment" '{node_ids:$nodes,direction:$direction,kind:$kind,pattern:$pattern,comment:$comment}')"
  response="$(curl -sS -w $'\n%{http_code}' -X POST "$BACKEND_URL/api/v1/blacklist" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$payload")"
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"
  printf '%s\n%s\n' "$status" "$body"
}

create_source_ip_blacklist() {
  local candidate payload_status body entry_id mutation
  for _ in $(seq 1 12); do
    candidate="$(find_allowed_source "$@")"
    {
      read -r payload_status
      body="$(cat)"
    } < <(create_blacklist ip source "$candidate" "phase1 node blacklist validation" "$@")
    if [[ "$payload_status" == "201" || "$payload_status" == "202" ]]; then
      entry_id="$(jq -r '.rule.id' <<<"$body")"
      [[ -n "$entry_id" && "$entry_id" != "null" ]]
      PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES:+${PHASE1_EXCLUDED_SOURCES},}${candidate}"
      mutation="$(jq -c . <<<"$body")"
      printf '%s\t%s\t%s\n' "$entry_id" "$candidate" "$mutation"
      return 0
    fi
    if [[ "$payload_status" == "409" ]]; then
      PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES:+${PHASE1_EXCLUDED_SOURCES},}${candidate}"
      continue
    fi
    printf 'Could not create node blacklist entry: %s\n' "$body" >&2
    return 1
  done
  printf 'Could not allocate a unique node blacklist test address.\n' >&2
  return 1
}

create_source_cidr_blacklist() {
  local octet network probe outside preview allowed payload_status body entry_id mutation
  for octet in $(seq 240 254); do
    network="198.18.${octet}.0/24"
    probe="198.18.${octet}.42"
    outside="198.18.$((octet == 254 ? 239 : octet + 1)).42"
    allowed=1
    for node_id in "$@"; do
      preview="$(preview_source "$node_id" "$probe")"
      if ! jq -e '.allowed == true and .reason == "allowed"' <<<"$preview" >/dev/null; then
        allowed=0
        break
      fi
    done
    [[ "$allowed" == "1" ]] || continue
    {
      read -r payload_status
      body="$(cat)"
    } < <(create_blacklist cidr source "$network" "phase1 source cidr validation" "$@")
    if [[ "$payload_status" == "201" || "$payload_status" == "202" ]]; then
      entry_id="$(jq -r '.rule.id' <<<"$body")"
      [[ -n "$entry_id" && "$entry_id" != "null" ]]
      mutation="$(jq -c . <<<"$body")"
      printf '%s\t%s\t%s\t%s\t%s\n' "$entry_id" "$network" "$probe" "$outside" "$mutation"
      return 0
    fi
    if [[ "$payload_status" != "409" ]]; then
      printf 'Could not create source CIDR blacklist entry: %s\n' "$body" >&2
      return 1
    fi
  done
  printf 'Could not allocate a unique source CIDR for validation.\n' >&2
  return 1
}

create_blacklist_pattern() {
  local kind="$1" direction="$2" pattern="$3" comment="$4"
  shift 4
  local payload_status body entry_id mutation
  {
    read -r payload_status
    body="$(cat)"
  } < <(create_blacklist "$kind" "$direction" "$pattern" "$comment" "$@")
  [[ "$payload_status" == "201" || "$payload_status" == "202" ]] || {
    printf 'Could not create %s %s blacklist entry: %s\n' "$direction" "$kind" "$body" >&2
    return 1
  }
  entry_id="$(jq -r '.rule.id' <<<"$body")"
  [[ -n "$entry_id" && "$entry_id" != "null" ]]
  mutation="$(jq -c . <<<"$body")"
  printf '%s\t%s\n' "$entry_id" "$mutation"
}

remember_rule_ids() {
  local mutation="$1" id
  while IFS= read -r id; do
    [[ -n "$id" && "$id" != "null" ]] && phase1_entry_ids+=("$id")
  done < <(jq -r '.rules[]?.id // empty' <<<"$mutation")
}

phase1_entry_ids=()
cleanup_phase1_source_blacklist() {
  local original_status="${1:-0}" cleanup_failed=0 entry_id mutation
  trap - EXIT
  set +e
  for entry_id in "${phase1_entry_ids[@]}"; do
    [[ -n "$entry_id" ]] || continue
    if mutation="$(curl -fsS -X DELETE "$BACKEND_URL/api/v1/blacklist/${entry_id}" -H "$AUTH_HEADER")"; then
      if ! wait_for_source_blacklist_mutation "$mutation"; then
        cleanup_failed=1
      fi
    else
      printf 'Could not remove phase 1 blacklist entry %s.\n' "$entry_id" >&2
      cleanup_failed=1
    fi
  done
  set -e
  if [[ "$cleanup_failed" == "1" ]]; then
    printf 'Phase 1 blacklist cleanup did not complete.\n' >&2
    return 1
  fi
  return "$original_status"
}

curl -fsS "$BACKEND_URL/healthz" >/dev/null
curl -fsS "$BACKEND_URL/readyz" >/dev/null
overview="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/overview")"
jq -e '.http_only == true and .sites == 5 and .nodes >= 2' <<<"$overview" >/dev/null

for node in codedev nuc; do
  site_slug="north"
  firewall_port=1080
  if [[ "$node" == "nuc" ]]; then
    site_slug="east"
    firewall_port=18081
  fi
  wait_for_node_in_sync "$node"
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  [[ "$(stat -c '%a' "$state_dir/monitor-state.json")" == "600" ]]
  [[ "$(stat -c '%a' "$state_dir/last-good-bundle.json")" == "600" ]]
  "$ROOT_DIR/singbox/sing-box" check -c "$state_dir/sing-box.json" >/dev/null
  nft -c -f "$state_dir/candidate.nft" >/dev/null
  jq -e '.inbounds | length == 1' "$state_dir/sing-box.json" >/dev/null
  jq -e 'all(.route.rules[]?; (.invert // false) != true)' "$state_dir/sing-box.json" >/dev/null
  rg -q "^    tcp dport ${firewall_port} accept$" "$state_dir/candidate.nft"
  ! rg -q "^    tcp dport ${firewall_port} drop$" "$state_dir/candidate.nft"

  release_id="$(jq -r '.release_id' "$TESTENV_DIR/release-${node}.json")"
  release="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/config/releases/${release_id}")"
  jq -e --arg node "$node" '.status == "succeeded" and .stage == "succeeded" and .progress == 100 and .node_ids == [$node]' <<<"$release" >/dev/null

  draft_id="$(jq -r '.id' "$TESTENV_DIR/draft-${node}.json")"
  node_id="$(<"$TESTENV_DIR/node-${node}.id")"
  site_id="$(<"$TESTENV_DIR/site-${site_slug}.id")"
  retry="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/releases" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -H "Idempotency-Key: phase1-source-baseline-${node}" -d "$(jq -nc --arg draft "$draft_id" --arg site "$site_id" --arg node "$node_id" '{draft_id:$draft,site_id:$site,node_ids:[$node],expected_current_version:null}')")"
  jq -e --arg release "$release_id" '.release_id == $release' <<<"$retry" >/dev/null
done

codedev_agent="codedev"
nuc_agent="nuc"

phase1_baseline_ip="$(find_allowed_source "$codedev_agent" "$nuc_agent")"
PHASE1_EXCLUDED_SOURCES="$phase1_baseline_ip"
trap 'cleanup_phase1_source_blacklist "$?"' EXIT

created_node_rule="$(create_source_ip_blacklist "$codedev_agent")"
IFS=$'\t' read -r phase1_node_entry_id phase1_node_ip phase1_node_mutation <<<"$created_node_rule"
[[ -n "$phase1_node_entry_id" && -n "$phase1_node_ip" ]]
remember_rule_ids "$phase1_node_mutation"
wait_for_source_blacklist_mutation "$phase1_node_mutation"
PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES},${phase1_node_ip}"

created_shared_rule="$(create_source_ip_blacklist "$codedev_agent" "$nuc_agent")"
IFS=$'\t' read -r phase1_shared_entry_id phase1_shared_ip phase1_shared_mutation <<<"$created_shared_rule"
[[ -n "$phase1_shared_entry_id" && -n "$phase1_shared_ip" ]]
remember_rule_ids "$phase1_shared_mutation"
wait_for_source_blacklist_mutation "$phase1_shared_mutation"
PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES},${phase1_shared_ip}"

created_cidr_rule="$(create_source_cidr_blacklist "$codedev_agent" "$nuc_agent")"
IFS=$'\t' read -r phase1_cidr_entry_id phase1_cidr phase1_cidr_probe phase1_cidr_outside_probe phase1_cidr_mutation <<<"$created_cidr_rule"
[[ -n "$phase1_cidr_entry_id" && -n "$phase1_cidr" ]]
remember_rule_ids "$phase1_cidr_mutation"
wait_for_source_blacklist_mutation "$phase1_cidr_mutation"

blacklist="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/blacklist")"
jq -e --arg id "$phase1_node_entry_id" --arg source "$phase1_node_ip" '
  any(.[]; .id == $id and .node_id == "codedev" and .direction == "source" and .kind == "ip" and .pattern == $source)
' <<<"$blacklist" >/dev/null
jq -e --arg source "$phase1_shared_ip" '
  any(.[]; .node_id == "codedev" and .direction == "source" and .kind == "ip" and .pattern == $source)
  and any(.[]; .node_id == "nuc" and .direction == "source" and .kind == "ip" and .pattern == $source)
' <<<"$blacklist" >/dev/null
jq -e --arg network "$phase1_cidr" '
  any(.[]; .node_id == "codedev" and .direction == "source" and .kind == "cidr" and .pattern == $network)
  and any(.[]; .node_id == "nuc" and .direction == "source" and .kind == "cidr" and .pattern == $network)
' <<<"$blacklist" >/dev/null

baseline_preview="$(preview_source "$codedev_agent" "$phase1_baseline_ip")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$baseline_preview" >/dev/null
node_blocked_preview="$(preview_source "$codedev_agent" "$phase1_node_ip")"
jq -e --arg source "$phase1_node_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source and any(.blacklist[]; .node_id == "codedev" and .pattern == $source)' <<<"$node_blocked_preview" >/dev/null ||
  jq -e --arg source "$phase1_node_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source' <<<"$node_blocked_preview" >/dev/null
shared_codedev_preview="$(preview_source "$codedev_agent" "$phase1_shared_ip")"
jq -e --arg source "$phase1_shared_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source' <<<"$shared_codedev_preview" >/dev/null
shared_nuc_preview="$(preview_source "$nuc_agent" "$phase1_shared_ip")"
jq -e --arg source "$phase1_shared_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source' <<<"$shared_nuc_preview" >/dev/null
cidr_codedev_preview="$(preview_source "$codedev_agent" "$phase1_cidr_probe")"
jq -e --arg network "$phase1_cidr" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $network' <<<"$cidr_codedev_preview" >/dev/null
cidr_nuc_preview="$(preview_source "$nuc_agent" "$phase1_cidr_probe")"
jq -e --arg network "$phase1_cidr" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $network' <<<"$cidr_nuc_preview" >/dev/null
cidr_outside_preview="$(preview_source "$codedev_agent" "$phase1_cidr_outside_probe")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$cidr_outside_preview" >/dev/null
node_elsewhere_preview="$(preview_source "$nuc_agent" "$phase1_node_ip")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$node_elsewhere_preview" >/dev/null

for node in codedev nuc; do
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  firewall_port=1080
  source_ips="$phase1_shared_ip"
  if [[ "$node" == "codedev" ]]; then
    source_ips="$phase1_node_ip $phase1_shared_ip"
  else
    firewall_port=18081
  fi
  [[ "$(stat -c '%a' "$state_dir/last-good-source-rules.json")" == "600" ]]
  for source_ip in $source_ips; do
    jq -e --arg source "$source_ip" '
      any(.route.rules[]?; .action == "reject" and (((.source_ip_cidr // []) | index($source)) != null or ((.source_ip_cidr // []) | index($source + "/32")) != null))
    ' "$state_dir/sing-box.json" >/dev/null
    jq -e --arg source "$source_ip" 'any(.rules[]?; .kind == "ip" and .pattern == $source)' "$state_dir/last-good-source-rules.json" >/dev/null
    rg -qF "ip saddr ${source_ip} tcp dport ${firewall_port} drop" "$state_dir/candidate.nft"
  done
  jq -e --arg network "$phase1_cidr" '
    any(.route.rules[]?; .action == "reject" and ((.source_ip_cidr // []) | index($network)) != null)
  ' "$state_dir/sing-box.json" >/dev/null
  jq -e --arg network "$phase1_cidr" 'any(.rules[]?; (.kind == "cidr" or .kind == "network") and .pattern == $network)' "$state_dir/last-good-source-rules.json" >/dev/null
  rg -qF "ip saddr ${phase1_cidr} tcp dport ${firewall_port} drop" "$state_dir/candidate.nft"
  jq -e 'any(.blacklist[]?; true) or (.blacklist | type == "array")' "$state_dir/last-good-bundle.json" >/dev/null
  jq -e --arg network "$phase1_cidr" 'any(.blacklist[]?; .kind == "cidr" and .pattern == $network)' "$state_dir/last-good-bundle.json" >/dev/null
done

phase1_domain="example.com"
domain_before_rules="$(jq -r '.rules[]? | select(.kind == "ip" or .kind == "cidr" or .kind == "network") | .pattern' "$TESTENV_DIR/monitor-codedev/state/last-good-source-rules.json")"
created_domain_rule="$(create_blacklist_pattern domain source "$phase1_domain" "phase1 source domain validation" "$codedev_agent")"
IFS=$'\t' read -r phase1_domain_entry_id phase1_domain_mutation <<<"$created_domain_rule"
remember_rule_ids "$phase1_domain_mutation"
wait_for_source_blacklist_mutation "$phase1_domain_mutation"

domain_bundle="$TESTENV_DIR/monitor-codedev/state/last-good-bundle.json"
domain_snapshot="$TESTENV_DIR/monitor-codedev/state/last-good-source-rules.json"
domain_config="$TESTENV_DIR/monitor-codedev/state/sing-box.json"
domain_nft="$TESTENV_DIR/monitor-codedev/state/candidate.nft"
jq -e --arg domain "$phase1_domain" '
  any(.blacklist[]?; .direction == "source" and .kind == "domain" and .pattern == $domain)
' "$domain_bundle" >/dev/null
domain_after_rules="$(jq -r '.rules[]? | select(.kind == "ip" or .kind == "cidr" or .kind == "network") | .pattern' "$domain_snapshot")"
domain_materialized_ips="$(comm -13 \
  <(printf '%s\n' "$domain_before_rules" | awk 'NF' | sort -u) \
  <(printf '%s\n' "$domain_after_rules" | awk 'NF' | sort -u))"
[[ -n "$domain_materialized_ips" ]] || {
  printf 'Source domain %s was not materialized into source IP rules.\n' "$phase1_domain" >&2
  exit 1
}
for source_ip in $domain_materialized_ips; do
  jq -e --arg source "$source_ip" '
    any(.route.rules[]?; .action == "reject" and (((.source_ip_cidr // []) | index($source)) != null or ((.source_ip_cidr // []) | index($source + "/32")) != null or ((.source_ip_cidr // []) | index($source + "/128")) != null))
  ' "$domain_config" >/dev/null
  nft_family="ip"
  [[ "$source_ip" == *:* ]] && nft_family="ip6"
  rg -qF "${nft_family} saddr ${source_ip} tcp dport 1080 drop" "$domain_nft"
done

phase1_dest_domain="ads.example"
created_dest_rule="$(create_blacklist_pattern domain destination "$phase1_dest_domain" "phase1 destination domain validation" "$codedev_agent")"
IFS=$'\t' read -r phase1_dest_entry_id phase1_dest_mutation <<<"$created_dest_rule"
remember_rule_ids "$phase1_dest_mutation"
wait_for_source_blacklist_mutation "$phase1_dest_mutation"
dest_preview="$(preview_dest "$codedev_agent" "tracker.ads.example")"
jq -e --arg dest "$phase1_dest_domain" '.allowed == false and .reason == "dest_blacklisted" and .matched_pattern == $dest' <<<"$dest_preview" >/dev/null
dest_elsewhere="$(preview_dest "$nuc_agent" "tracker.ads.example")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$dest_elsewhere" >/dev/null
jq -e --arg dest "$phase1_dest_domain" '
  any(.route.rules[]?; .action == "reject" and ((.domain_suffix // []) | index($dest)) != null)
' "$TESTENV_DIR/monitor-codedev/state/sing-box.json" >/dev/null
! rg -qF "ads.example" "$TESTENV_DIR/monitor-codedev/state/candidate.nft"
! jq -e --arg dest "$phase1_dest_domain" '
  any(.route.rules[]?; .action == "reject" and ((.domain_suffix // []) | index($dest)) != null)
' "$TESTENV_DIR/monitor-nuc/state/sing-box.json" >/dev/null

cleanup_phase1_source_blacklist 0
trap - EXIT

for node in codedev nuc; do
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  for source_ip in "$phase1_node_ip" "$phase1_shared_ip" "$phase1_cidr"; do
    if jq -e --arg source "$source_ip" 'any(.route.rules[]?; ((.source_ip_cidr // []) | index($source) or index($source + "/32")))' "$state_dir/sing-box.json" >/dev/null; then
      printf 'Removed blacklist address %s is still rendered for %s.\n' "$source_ip" "$node" >&2
      exit 1
    fi
  done
done

audit="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/audit/verify")"
jq -e '.valid == true and .event_count > 0' <<<"$audit" >/dev/null

codedev_release="$(jq -r '.release_id' "$TESTENV_DIR/release-codedev.json")"
cross_node_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BACKEND_URL/agent/v1/ack" -H "Authorization: Bearer $(<"$TESTENV_DIR/nuc.token")" -H 'Content-Type: application/json' -d "$(jq -nc --arg release "$codedev_release" '{node_id:"nuc",release_id:$release,desired_version:999,applied_version:0,bundle_hash:"forged",applied_hash:"",ok:false,sequence:999999}')")"
[[ "$cross_node_status" == "409" ]]

for port in 1080 18081 19090 19091; do
  nc -z 127.0.0.1 "$port"
done
jq -e '.inbounds[0].listen == "0.0.0.0" and .inbounds[0].listen_port == 1080' \
  "$TESTENV_DIR/monitor-codedev/state/sing-box.json" >/dev/null
jq -e '.inbounds[0].listen == "0.0.0.0" and .inbounds[0].listen_port == 18081' \
  "$TESTENV_DIR/monitor-nuc/state/sing-box.json" >/dev/null
frontend_port="${GROUPROXY_TEST_FRONTEND_PORT:-3000}"
ss -ltnH "( sport = :${frontend_port} )" | awk -v port="$frontend_port" '$4 == "0.0.0.0:" port { found=1 } END { exit !found }'
frontend_html="$(curl -fsS "http://127.0.0.1:${frontend_port}/")"
rg -qi 'grouproxy' <<<"$frontend_html"
printf 'Phase 0/1 validation passed.\n%s\n' "$overview"
