#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
BACKEND_PORT="${GROUPROXY_TEST_BACKEND_PORT:-8000}"
FRONTEND_PORT="${GROUPROXY_TEST_FRONTEND_PORT:-3000}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
MONGODB_URL_OVERRIDE="${GROUPROXY_TEST_MONGODB_URL:-}"
MONGODB_DATABASE_OVERRIDE="${GROUPROXY_TEST_MONGODB_DATABASE:-grouproxy_test}"
GQUAN_DELIVERY_MODE="${GROUPROXY_TEST_GQUAN_DELIVERY_MODE:-app}"
GQUAN_APP_TOKEN="${GROUPROXY_TEST_GQUAN_APP_TOKEN:-}"
GQUAN_TEST_CODE="${GROUPROXY_TEST_GQUAN_CODE:-123456}"
PROXY_ACCESS_FQDN="${GROUPROXY_TEST_PROXY_ACCESS_FQDN:-proxy.1oa.com.cn}"

case "$GQUAN_DELIVERY_MODE" in
  app)
    [[ -n "$GQUAN_APP_TOKEN" ]] || {
      printf 'Set GROUPROXY_TEST_GQUAN_APP_TOKEN to enable real GQuan verification delivery.\n' >&2
      exit 1
    }
    ;;
  stub)
    ;;
  *)
    printf 'GROUPROXY_TEST_GQUAN_DELIVERY_MODE must be app or stub.\n' >&2
    exit 1
    ;;
esac

# Do not pass the test-specific name on to the backend process. In app mode,
# the standard setting is exported only in the uvicorn process environment.
unset GROUPROXY_TEST_GQUAN_APP_TOKEN

if [[ "${GROUPROXY_TESTENV_RESET:-0}" == "1" ]]; then
  # Stop every service before removing its state directory. This avoids
  # orphaned monitor/sing-box processes continuing to use deleted files.
  if [[ -x "$ROOT_DIR/scripts/testenv-down.sh" && -d "$TESTENV_DIR" ]]; then
    GROUPROXY_TESTENV_DIR="$TESTENV_DIR" "$ROOT_DIR/scripts/testenv-down.sh" >/dev/null 2>&1 || true
  fi
  for port in "$BACKEND_PORT" "$FRONTEND_PORT" 18080 18081 19090 19091; do
    if nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
      printf 'Test port %s is still in use after cleanup; choose another port or stop its owner.\n' "$port" >&2
      exit 1
    fi
  done
  if [[ -d "$TESTENV_DIR" ]]; then
    find "$TESTENV_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  fi
fi

mkdir -p "$TESTENV_DIR"/{state,logs,backups,monitor-codedev,monitor-nuc}
chmod 700 "$TESTENV_DIR" "$TESTENV_DIR"/{state,logs,backups,monitor-codedev,monitor-nuc}
ENV_FILE="$TESTENV_DIR/backend.env"

if [[ ! -f "$ENV_FILE" ]]; then
  [[ -n "$MONGODB_URL_OVERRIDE" ]] || {
    printf 'Set GROUPROXY_TEST_MONGODB_URL to the codedev MongoDB URI before starting tests.\n' >&2
    exit 1
  }
  umask 077
  management_token="${GROUPROXY_TEST_MANAGEMENT_TOKEN:-$(openssl rand -hex 24)}"
  bundle_secret="${GROUPROXY_TEST_BUNDLE_HMAC_SECRET:-$(openssl rand -hex 32)}"
  proxy_credential_secret="${GROUPROXY_TEST_PROXY_CREDENTIAL_SECRET:-$(openssl rand -hex 32)}"
  backup_encryption_key="${GROUPROXY_TEST_BACKUP_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
  admin_password="${GROUPROXY_TEST_ADMIN_PASSWORD:-$(openssl rand -hex 24)}"
  {
    printf 'GROUPROXY_ENVIRONMENT=test\n'
    printf 'GROUPROXY_MONGODB_URL=%q\n' "$MONGODB_URL_OVERRIDE"
    printf 'GROUPROXY_MONGODB_DATABASE=%q\n' "$MONGODB_DATABASE_OVERRIDE"
    printf 'GROUPROXY_HOST=127.0.0.1\n'
    printf 'GROUPROXY_PORT=%s\n' "$BACKEND_PORT"
    printf 'GROUPROXY_BACKEND_PUBLIC_URL=%s\n' "$BACKEND_URL"
    printf 'GROUPROXY_PROXY_ACCESS_FQDN=%s\n' "$PROXY_ACCESS_FQDN"
    printf 'GROUPROXY_BUNDLE_HMAC_SECRET=%s\n' "$bundle_secret"
    printf 'GROUPROXY_PROXY_CREDENTIAL_SECRET=%s\n' "$proxy_credential_secret"
    printf 'GROUPROXY_BACKUP_DIRECTORY=%q\n' "$TESTENV_DIR/backups"
    printf 'GROUPROXY_BACKUP_ENCRYPTION_KEY=%q\n' "$backup_encryption_key"
    # Keep archive creation explicit in the shared test database. Phase 4
    # creates a manual encrypted archive and runs a non-destructive rehearsal.
    printf 'GROUPROXY_BACKUP_AUTO_ENABLED=false\n'
    printf 'GROUPROXY_ADMIN_USERNAME=admin\n'
    printf 'GROUPROXY_ADMIN_PASSWORD=%s\n' "$admin_password"
    printf 'GROUPROXY_MANAGEMENT_TOKEN=%s\n' "$management_token"
    printf 'GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true\n'
    printf 'GROUPROXY_SUBSCRIPTION_INLINE_MAX_BYTES=64\n'
    # Keep shared test data-plane validation operator-driven. Automatic probes
    # target public sites and are inappropriate here unless explicitly enabled.
    printf 'GROUPROXY_PROBE_AUTO_ENABLED=false\n'
    printf 'GROUPROXY_GQUAN_DELIVERY_MODE=%q\n' "$GQUAN_DELIVERY_MODE"
    if [[ "$GQUAN_DELIVERY_MODE" == "stub" ]]; then
      printf 'GROUPROXY_GQUAN_TEST_CODE=%s\n' "$GQUAN_TEST_CODE"
    fi
    printf 'GROUPROXY_SEED_DEFAULT_SITES=true\n'
  } > "$ENV_FILE"
fi

# Phase 4 credentials must remain derivable after a test environment restart.
# Existing runtime files predate this setting, so add one once without printing
# or replacing any of their protected values.
if ! rg -q '^GROUPROXY_PROXY_CREDENTIAL_SECRET=' "$ENV_FILE"; then
  umask 077
  printf 'GROUPROXY_PROXY_CREDENTIAL_SECRET=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
if ! rg -q '^GROUPROXY_PROXY_ACCESS_FQDN=' "$ENV_FILE"; then
  printf 'GROUPROXY_PROXY_ACCESS_FQDN=%s\n' "$PROXY_ACCESS_FQDN" >> "$ENV_FILE"
fi
if ! rg -q '^GROUPROXY_PROBE_AUTO_ENABLED=' "$ENV_FILE"; then
  printf 'GROUPROXY_PROBE_AUTO_ENABLED=false\n' >> "$ENV_FILE"
fi
if ! rg -q '^GROUPROXY_BACKUP_DIRECTORY=' "$ENV_FILE"; then
  printf 'GROUPROXY_BACKUP_DIRECTORY=%q\n' "$TESTENV_DIR/backups" >> "$ENV_FILE"
fi
if ! rg -q '^GROUPROXY_BACKUP_ENCRYPTION_KEY=' "$ENV_FILE"; then
  umask 077
  printf 'GROUPROXY_BACKUP_ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
if ! rg -q '^GROUPROXY_BACKUP_AUTO_ENABLED=' "$ENV_FILE"; then
  printf 'GROUPROXY_BACKUP_AUTO_ENABLED=false\n' >> "$ENV_FILE"
fi

set -a
source "$ENV_FILE"
set +a

if [[ -n "$MONGODB_URL_OVERRIDE" && "$GROUPROXY_MONGODB_URL" != "$MONGODB_URL_OVERRIDE" ]]; then
  printf 'Test environment already points at another MongoDB URI. Reset it before changing targets.\n' >&2
  exit 1
fi
if [[ "$GROUPROXY_GQUAN_DELIVERY_MODE" != "$GQUAN_DELIVERY_MODE" ]]; then
  [[ "${GROUPROXY_TESTENV_RECONFIGURE_GQUAN:-0}" == "1" ]] || {
    printf 'Test environment uses another GQuan delivery mode. Stop it, then rerun with GROUPROXY_TESTENV_RECONFIGURE_GQUAN=1.\n' >&2
    exit 1
  }
  if nc -z 127.0.0.1 "$BACKEND_PORT" >/dev/null 2>&1; then
    printf 'Stop the current test environment before changing its GQuan delivery mode.\n' >&2
    exit 1
  fi
  sed -i -E "s/^GROUPROXY_GQUAN_DELIVERY_MODE=.*/GROUPROXY_GQUAN_DELIVERY_MODE=${GQUAN_DELIVERY_MODE}/" "$ENV_FILE"
  if [[ "$GQUAN_DELIVERY_MODE" == "app" ]]; then
    sed -i -E '/^GROUPROXY_GQUAN_TEST_CODE=/d' "$ENV_FILE"
  elif rg -q '^GROUPROXY_GQUAN_TEST_CODE=' "$ENV_FILE"; then
    sed -i -E "s/^GROUPROXY_GQUAN_TEST_CODE=.*/GROUPROXY_GQUAN_TEST_CODE=${GQUAN_TEST_CODE}/" "$ENV_FILE"
  else
    printf 'GROUPROXY_GQUAN_TEST_CODE=%s\n' "$GQUAN_TEST_CODE" >> "$ENV_FILE"
  fi
  set -a
  source "$ENV_FILE"
  set +a
fi
if [[ "$GROUPROXY_MONGODB_URL" == mongodb://127.0.0.1:27018* ]]; then
  printf 'The retired isolated MongoDB target is not allowed. Reset the test environment with the codedev URI.\n' >&2
  exit 1
fi

# Do not start partial local services when the configured shared database is
# unreachable or its credentials are invalid. The URI stays in the protected
# runtime environment and is intentionally not included in diagnostics.
if ! "$ROOT_DIR/.venv/bin/python" - <<'PY'
import os
import sys

from pymongo import MongoClient

client = None
try:
    client = MongoClient(
        os.environ["GROUPROXY_MONGODB_URL"],
        connectTimeoutMS=5_000,
        serverSelectionTimeoutMS=5_000,
    )
    client.get_database(os.environ["GROUPROXY_MONGODB_DATABASE"]).command("ping")
except Exception:
    sys.exit(1)
finally:
    if client is not None:
        client.close()
PY
then
  printf 'Cannot reach or authenticate to the configured codedev MongoDB target. Check the URI or SSH tunnel, then retry.\n' >&2
  exit 1
fi

if ! nc -z 127.0.0.1 "$BACKEND_PORT" >/dev/null 2>&1; then
  (
    cd "$ROOT_DIR/backend"
    if [[ "$GROUPROXY_GQUAN_DELIVERY_MODE" == "app" ]]; then
      export GROUPROXY_GQUAN_APP_TOKEN="$GQUAN_APP_TOKEN"
    fi
    nohup setsid "$ROOT_DIR/.venv/bin/uvicorn" main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
      </dev/null >"$TESTENV_DIR/logs/backend.log" 2>&1 &
    printf '%s\n' "$!" > "$TESTENV_DIR/backend.pid"
  )
fi

for _ in $(seq 1 60); do
  if curl -fsS "$BACKEND_URL/readyz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "$BACKEND_URL/readyz" >/dev/null

bootstrap_node() {
  local slug="$1" name="$2" port="$3" cidr="$4" extra_cidr="$5" api_port="$6"
  local sites site_id nodes node_id response draft draft_id release
  sites="$(curl -fsS -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" "$BACKEND_URL/api/v1/sites")"
  site_id="$(jq -r --arg slug "$slug" '.[] | select(.slug == $slug) | .id' <<<"$sites" | head -n 1)"
  [[ -n "$site_id" && "$site_id" != "null" ]] || { printf 'site %s not found\n' "$slug" >&2; exit 1; }
  printf '%s\n' "$site_id" > "$TESTENV_DIR/site-${slug}.id"

  nodes="$(curl -fsS -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" "$BACKEND_URL/api/v1/nodes")"
  node_id="$(jq -r --arg agent "$name" '.[] | select(.agent_id == $agent) | .id' <<<"$nodes" | head -n 1)"
  if [[ -z "$node_id" || "$node_id" == "null" ]]; then
    response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/nodes" -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$site_id" --arg name "$name" --arg agent "$name" '{site_id:$site,name:$name,agent_id:$agent,advertise_ip:"127.0.0.1"}')")"
    node_id="$(jq -r '.id' <<<"$response")"
    jq -r '.agent_token' <<<"$response" > "$TESTENV_DIR/${name}.token"
    chmod 600 "$TESTENV_DIR/${name}.token"
  elif [[ ! -s "$TESTENV_DIR/${name}.token" ]]; then
    printf 'node %s exists but its one-time token file is missing; refusing to rotate it implicitly\n' "$name" >&2
    exit 1
  fi
  printf '%s\n' "$node_id" > "$TESTENV_DIR/node-${name}.id"

  for policy_cidr in "$cidr" "$extra_cidr"; do
    if ! curl -fsS -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" "$BACKEND_URL/api/v1/sites/${site_id}/cidrs" | jq -e --arg cidr "$policy_cidr" '.[] | select(.cidr == $cidr)' >/dev/null; then
      curl -fsS -X POST "$BACKEND_URL/api/v1/sites/${site_id}/cidrs" -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" -H 'Content-Type: application/json' -d "$(jq -nc --arg cidr "$policy_cidr" '{cidr:$cidr,comment:"phase1 test policy"}')" >/dev/null
    fi
  done

  draft="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/drafts" -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" -H 'Content-Type: application/json' -d "$(jq -nc --arg site "$site_id" --arg node "$node_id" --arg c1 "$cidr" --arg c2 "$extra_cidr" '{site_id:$site,node_ids:[$node],diff:{allow_cidrs:[$c1,$c2],http_only:true},note:"phase1 local validation"}')")"
  draft_id="$(jq -r '.id' <<<"$draft")"
  jq . <<<"$draft" > "$TESTENV_DIR/draft-${name}.json"
  release="$(curl -fsS -X POST "$BACKEND_URL/api/v1/config/releases" -H "Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}" -H 'Content-Type: application/json' -H "Idempotency-Key: phase1-${name}" -d "$(jq -nc --arg draft "$draft_id" --arg site "$site_id" --arg node "$node_id" '{draft_id:$draft,site_id:$site,node_ids:[$node],expected_current_version:null}')")"
  jq . <<<"$release" > "$TESTENV_DIR/release-${name}.json"

  local state_dir="$TESTENV_DIR/monitor-${name}"
  umask 077
  cat > "$state_dir/monitor.yaml" <<EOF
backend_url: "$BACKEND_URL"
node_id: "$name"
token_file: "$TESTENV_DIR/${name}.token"
state_dir: "$state_dir/state"
singbox_bin: "$ROOT_DIR/singbox/sing-box"
singbox_config: "$state_dir/state/sing-box.json"
listen_port: $port
listen_port_override: $port
firewall_mode: dry-run
poll_interval_seconds: 2
heartbeat_interval_seconds: 2
proxy_config_interval_seconds: 2
run_singbox: true
hmac_secret: "${GROUPROXY_BUNDLE_HMAC_SECRET}"
allow_insecure_http: true
health_window_seconds: 2
health_sample_seconds: 1
clash_api_listen: "127.0.0.1:${api_port}"
EOF
  if [[ -f "$state_dir/monitor.pid" ]] && kill -0 "$(<"$state_dir/monitor.pid")" 2>/dev/null; then
    return
  fi
  nohup setsid "$ROOT_DIR/monitor/dist/grouproxy-monitor-linux-amd64" -config "$state_dir/monitor.yaml" \
    </dev/null >"$TESTENV_DIR/logs/monitor-${name}.log" 2>&1 &
  printf '%s\n' "$!" > "$state_dir/monitor.pid"
}

if [[ ! -x "$ROOT_DIR/monitor/dist/grouproxy-monitor-linux-amd64" ]]; then
  (cd "$ROOT_DIR/monitor" && make dist)
fi

bootstrap_node north codedev 18080 10.32.12.0/24 127.0.0.1/32 19090
bootstrap_node east nuc 18081 10.32.13.0/24 127.0.0.1/32 19091

if [[ "${GROUPROXY_START_FRONTEND:-1}" == "1" ]] && ! nc -z 127.0.0.1 "$FRONTEND_PORT" >/dev/null 2>&1; then
  (
    cd "$ROOT_DIR/frontend"
    if [[ ! -d node_modules ]]; then npm install --no-audit --no-fund; fi
    nohup setsid env NEXT_PUBLIC_API_BASE_URL=/backend-api GROUPROXY_BACKEND_API_URL="$BACKEND_URL" npm run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT" \
      </dev/null >"$TESTENV_DIR/logs/frontend.log" 2>&1 &
    printf '%s\n' "$!" > "$TESTENV_DIR/frontend.pid"
  )
fi

if [[ "${GROUPROXY_START_FRONTEND:-1}" == "1" ]]; then
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  curl -fsS "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null
fi

printf 'Test environment is starting. Backend: %s  Frontend: http://127.0.0.1:%s\n' "$BACKEND_URL" "$FRONTEND_PORT"
printf 'Shared MongoDB database: %s\n' "$GROUPROXY_MONGODB_DATABASE"
printf 'Run scripts/verify-phase1.sh after monitor ACKs arrive.\n'
