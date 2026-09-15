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
      printf 'Automatic source-blacklist release %s did not complete.\n' "$release_id" >&2
      return 1
    fi
  done
  mapfile -t node_ids < <(jq -r '.distribution[] | select(.state == "released") | .node_ids[]?' <<<"$mutation" | sort -u)
  for node in "${node_ids[@]}"; do
    wait_for_node_in_sync "$node"
  done
}

# Pick an address which is presently allowed by each supplied site. The test
# database is shared, so this avoids assuming that no unrelated deny rule has
# been configured by another test run or an operator.
PHASE1_EXCLUDED_SOURCES=""
find_allowed_source() {
  local prefix host candidate site_id preview allowed
  for prefix in 192.0.2 198.51.100 203.0.113 198.18.240; do
    for host in $(seq 1 254); do
      candidate="${prefix}.${host}"
      [[ ",${PHASE1_EXCLUDED_SOURCES}," == *",${candidate},"* ]] && continue
      allowed=1
      for site_id in "$@"; do
        preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$site_id" --arg source "$candidate" '{site_id:$site,source_ip:$source}')")"
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
  printf 'Could not find an address allowed by every requested test site.\n' >&2
  return 1
}

create_source_blacklist() {
  local scope="$1" target_site_id="$2"
  shift 2
  local candidate payload response status body entry_id mutation
  for _ in $(seq 1 12); do
    candidate="$(find_allowed_source "$@")"
    if [[ "$scope" == "global" ]]; then
      payload="$(jq -nc --arg pattern "$candidate" --arg comment "phase1 source blacklist validation" '{scope:"global",kind:"ip",pattern:$pattern,comment:$comment}')"
    else
      payload="$(jq -nc --arg site "$target_site_id" --arg pattern "$candidate" --arg comment "phase1 source blacklist validation" '{scope:"site",site_id:$site,kind:"ip",pattern:$pattern,comment:$comment}')"
    fi
    response="$(curl -sS -w $'\n%{http_code}' -X POST "$BACKEND_URL/api/v1/source-blacklist" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$payload")"
    status="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$status" == "201" || "$status" == "202" ]]; then
      entry_id="$(jq -r '.rule.id' <<<"$body")"
      [[ -n "$entry_id" && "$entry_id" != "null" ]]
      PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES:+${PHASE1_EXCLUDED_SOURCES},}${candidate}"
      mutation="$(jq -c . <<<"$body")"
      printf '%s\t%s\t%s\n' "$entry_id" "$candidate" "$mutation"
      return 0
    fi
    if [[ "$status" == "409" ]]; then
      # A disabled duplicate is not reflected in preview; avoid it and retry.
      PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES:+${PHASE1_EXCLUDED_SOURCES},}${candidate}"
      continue
    fi
    printf 'Could not create %s source blacklist entry: %s\n' "$scope" "$body" >&2
    return 1
  done
  printf 'Could not allocate a unique %s source blacklist test address.\n' "$scope" >&2
  return 1
}

create_source_network_blacklist() {
  local north_site_id="$1" east_site_id="$2"
  local octet network probe outside preview allowed site_id payload response status body entry_id mutation
  for octet in $(seq 240 254); do
    network="198.18.${octet}.0/24"
    probe="198.18.${octet}.42"
    outside="198.18.$((octet == 254 ? 239 : octet + 1)).42"
    allowed=1
    for site_id in "$north_site_id" "$east_site_id"; do
      preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$site_id" --arg source "$probe" '{site_id:$site,source_ip:$source}')")"
      if ! jq -e '.allowed == true and .reason == "allowed"' <<<"$preview" >/dev/null; then
        allowed=0
        break
      fi
    done
    [[ "$allowed" == "1" ]] || continue
    payload="$(jq -nc --arg pattern "$network" --arg comment "phase1 source network validation" '{scope:"global",kind:"network",pattern:$pattern,comment:$comment}')"
    response="$(curl -sS -w $'\n%{http_code}' -X POST "$BACKEND_URL/api/v1/source-blacklist" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$payload")"
    status="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$status" == "201" || "$status" == "202" ]]; then
      entry_id="$(jq -r '.rule.id' <<<"$body")"
      [[ -n "$entry_id" && "$entry_id" != "null" ]]
      mutation="$(jq -c . <<<"$body")"
      printf '%s\t%s\t%s\t%s\t%s\n' "$entry_id" "$network" "$probe" "$outside" "$mutation"
      return 0
    fi
    if [[ "$status" != "409" ]]; then
      printf 'Could not create source network blacklist entry: %s\n' "$body" >&2
      return 1
    fi
  done
  printf 'Could not allocate a unique global source network for validation.\n' >&2
  return 1
}

create_source_domain_blacklist() {
  local site_id="$1" domain="$2" payload response status body entry_id mutation
  payload="$(jq -nc --arg site "$site_id" --arg pattern "$domain" --arg comment "phase1 source domain validation" '{scope:"site",site_id:$site,kind:"domain",pattern:$pattern,comment:$comment}')"
  response="$(curl -sS -w $'\n%{http_code}' -X POST "$BACKEND_URL/api/v1/source-blacklist" -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$payload")"
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"
  [[ "$status" == "201" || "$status" == "202" ]] || {
    printf 'Could not create source domain blacklist entry: %s\n' "$body" >&2
    return 1
  }
  entry_id="$(jq -r '.rule.id' <<<"$body")"
  [[ -n "$entry_id" && "$entry_id" != "null" ]]
  mutation="$(jq -c . <<<"$body")"
  printf '%s\t%s\n' "$entry_id" "$mutation"
}

phase1_site_entry_id=""
phase1_global_entry_id=""
phase1_network_entry_id=""
phase1_domain_entry_id=""
cleanup_phase1_source_blacklist() {
  local original_status="${1:-0}" cleanup_failed=0 entry_id mutation
  trap - EXIT
  set +e
  for entry_id in "$phase1_site_entry_id" "$phase1_global_entry_id" "$phase1_network_entry_id" "$phase1_domain_entry_id"; do
    [[ -n "$entry_id" ]] || continue
    if mutation="$(curl -fsS -X DELETE "$BACKEND_URL/api/v1/source-blacklist/${entry_id}" -H "$AUTH_HEADER")"; then
      if ! wait_for_source_blacklist_mutation "$mutation"; then
        cleanup_failed=1
      fi
    else
      printf 'Could not remove phase 1 source blacklist entry %s.\n' "$entry_id" >&2
      cleanup_failed=1
    fi
  done
  set -e
  if [[ "$cleanup_failed" == "1" ]]; then
    printf 'Phase 1 source blacklist cleanup did not complete.\n' >&2
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
  # A clean bundle has no inverse source allowlist. The nft baseline must
  # explicitly leave the proxy port open instead of ending in a catch-all drop.
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

north_id="$(<"$TESTENV_DIR/site-north.id")"
east_id="$(<"$TESTENV_DIR/site-east.id")"
codedev_node_id="$(<"$TESTENV_DIR/node-codedev.id")"
nuc_node_id="$(<"$TESTENV_DIR/node-nuc.id")"

# Verify the default first, then add isolated site/global entries. Each
# mutation returns its ordinary signed releases, which are awaited before the
# next mutation so no desired bundle supersedes an in-flight release.
phase1_baseline_ip="$(find_allowed_source "$north_id" "$east_id")"
PHASE1_EXCLUDED_SOURCES="$phase1_baseline_ip"
trap 'cleanup_phase1_source_blacklist "$?"' EXIT

created_site_rule="$(create_source_blacklist site "$north_id" "$north_id")"
IFS=$'\t' read -r phase1_site_entry_id phase1_site_ip phase1_site_mutation <<<"$created_site_rule"
[[ -n "$phase1_site_entry_id" && -n "$phase1_site_ip" ]]
wait_for_source_blacklist_mutation "$phase1_site_mutation"
PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES},${phase1_site_ip}"

created_global_rule="$(create_source_blacklist global "" "$north_id" "$east_id")"
IFS=$'\t' read -r phase1_global_entry_id phase1_global_ip phase1_global_mutation <<<"$created_global_rule"
[[ -n "$phase1_global_entry_id" && -n "$phase1_global_ip" ]]
wait_for_source_blacklist_mutation "$phase1_global_mutation"
PHASE1_EXCLUDED_SOURCES="${PHASE1_EXCLUDED_SOURCES},${phase1_global_ip}"

# Use an available RFC 2544 network. The helper avoids active user rules and
# disabled duplicates in the shared test database.
created_network_rule="$(create_source_network_blacklist "$north_id" "$east_id")"
IFS=$'\t' read -r phase1_network_entry_id phase1_network phase1_network_probe phase1_network_outside_probe phase1_network_mutation <<<"$created_network_rule"
[[ -n "$phase1_network_entry_id" && -n "$phase1_network" ]]
wait_for_source_blacklist_mutation "$phase1_network_mutation"

source_blacklist="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/source-blacklist")"
jq -e --arg id "$phase1_site_entry_id" --arg site "$north_id" --arg source "$phase1_site_ip" '
  any(.[]; .id == $id and .scope == "site" and .site_id == $site and .kind == "ip" and .pattern == $source)
' <<<"$source_blacklist" >/dev/null
jq -e --arg id "$phase1_global_entry_id" --arg source "$phase1_global_ip" '
  any(.[]; .id == $id and .scope == "global" and .site_id == null and .kind == "ip" and .pattern == $source)
' <<<"$source_blacklist" >/dev/null
jq -e --arg id "$phase1_network_entry_id" --arg network "$phase1_network" '
  any(.[]; .id == $id and .scope == "global" and .site_id == null and .kind == "network" and .pattern == $network)
' <<<"$source_blacklist" >/dev/null

baseline_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$north_id" --arg source "$phase1_baseline_ip" '{site_id:$site,source_ip:$source}')")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$baseline_preview" >/dev/null
site_blocked_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$north_id" --arg source "$phase1_site_ip" '{site_id:$site,source_ip:$source}')")"
jq -e --arg source "$phase1_site_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source and any(.source_blacklist[]; .scope == "site" and .pattern == $source)' <<<"$site_blocked_preview" >/dev/null
global_north_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$north_id" --arg source "$phase1_global_ip" '{site_id:$site,source_ip:$source}')")"
jq -e --arg source "$phase1_global_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source and any(.source_blacklist[]; .scope == "global" and .pattern == $source)' <<<"$global_north_preview" >/dev/null
global_east_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$east_id" --arg source "$phase1_global_ip" '{site_id:$site,source_ip:$source}')")"
jq -e --arg source "$phase1_global_ip" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $source and any(.source_blacklist[]; .scope == "global" and .pattern == $source)' <<<"$global_east_preview" >/dev/null
network_north_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$north_id" --arg source "$phase1_network_probe" '{site_id:$site,source_ip:$source}')")"
jq -e --arg network "$phase1_network" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $network and any(.source_blacklist[]; .scope == "global" and .kind == "network" and .pattern == $network)' <<<"$network_north_preview" >/dev/null
network_east_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$east_id" --arg source "$phase1_network_probe" '{site_id:$site,source_ip:$source}')")"
jq -e --arg network "$phase1_network" '.allowed == false and .reason == "source_blacklisted" and .matched_pattern == $network' <<<"$network_east_preview" >/dev/null
network_outside_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$north_id" --arg source "$phase1_network_outside_probe" '{site_id:$site,source_ip:$source}')")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$network_outside_preview" >/dev/null
site_elsewhere_preview="$(curl -fsS -H "$AUTH_HEADER" -X POST "$BACKEND_URL/api/v1/source-blacklist/preview" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$east_id" --arg source "$phase1_site_ip" '{site_id:$site,source_ip:$source}')")"
jq -e '.allowed == true and .reason == "allowed"' <<<"$site_elsewhere_preview" >/dev/null

for node in codedev nuc; do
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  firewall_port=1080
  source_ips="$phase1_global_ip"
  if [[ "$node" == "codedev" ]]; then
    source_ips="$phase1_site_ip $phase1_global_ip"
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
  jq -e --arg network "$phase1_network" '
    any(.route.rules[]?; .action == "reject" and ((.source_ip_cidr // []) | index($network)) != null)
  ' "$state_dir/sing-box.json" >/dev/null
  jq -e --arg network "$phase1_network" 'any(.rules[]?; .kind == "network" and .pattern == $network)' "$state_dir/last-good-source-rules.json" >/dev/null
  rg -qF "ip saddr ${phase1_network} tcp dport ${firewall_port} drop" "$state_dir/candidate.nft"
done

# Domain rules are resolved by the monitor and persisted as concrete source
# IP entries. Verify the live bundle, materialized snapshot, sing-box route,
# and nftables candidate instead of relying on an unsupported source_domain
# matcher in sing-box.
phase1_domain="example.com"
domain_before_rules="$(jq -r '.rules[]? | select(.kind == "ip" or .kind == "network") | .pattern' "$TESTENV_DIR/monitor-codedev/state/last-good-source-rules.json")"
created_domain_rule="$(create_source_domain_blacklist "$north_id" "$phase1_domain")"
IFS=$'\t' read -r phase1_domain_entry_id phase1_domain_mutation <<<"$created_domain_rule"
wait_for_source_blacklist_mutation "$phase1_domain_mutation"

domain_bundle="$TESTENV_DIR/monitor-codedev/state/last-good-bundle.json"
domain_snapshot="$TESTENV_DIR/monitor-codedev/state/last-good-source-rules.json"
domain_config="$TESTENV_DIR/monitor-codedev/state/sing-box.json"
domain_nft="$TESTENV_DIR/monitor-codedev/state/candidate.nft"
jq -e --arg domain "$phase1_domain" '
  any(.source_blacklist[]?; .scope == "site" and .kind == "domain" and .pattern == $domain)
' "$domain_bundle" >/dev/null
domain_after_rules="$(jq -r '.rules[]? | select(.kind == "ip" or .kind == "network") | .pattern' "$domain_snapshot")"
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

cleanup_phase1_source_blacklist 0
trap - EXIT

for node in codedev nuc; do
  state_dir="$TESTENV_DIR/monitor-${node}/state"
  for source_ip in "$phase1_site_ip" "$phase1_global_ip" "$phase1_network"; do
    if jq -e --arg source "$source_ip" 'any(.route.rules[]?; ((.source_ip_cidr // []) | index($source) or index($source + "/32")))' "$state_dir/sing-box.json" >/dev/null; then
      printf 'Removed source blacklist address %s is still rendered for %s.\n' "$source_ip" "$node" >&2
      exit 1
    fi
  done
done

audit="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/audit/verify")"
jq -e '.valid == true and .event_count > 0' <<<"$audit" >/dev/null

# A node cannot ACK a release generated for a different node, even with a
# syntactically valid request and a newer sequence number.
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
