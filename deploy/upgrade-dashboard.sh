#!/usr/bin/env bash
set -Eeuo pipefail

# This script is installed as /opt/one_proxy/upgrade-dashboard.sh. It never
# pulls source or configuration from a remote repository.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$ROOT_DIR/src"
ENV_FILE="$ROOT_DIR/etc/backend.env"
COMPOSE_FILE="$SOURCE_DIR/docker-compose.yaml"
PROJECT_NAME="grouproxy"
FRONTEND_BACKEND_URL="http://host.docker.internal:8000"
DRY_RUN=0
ROLLBACK_NEEDED=0
NATIVE_BACKEND_ACTIVE=0
NATIVE_DASHBOARD_ACTIVE=0

log() {
  printf '[upgrade-dashboard] %s\n' "$*"
}

die() {
  printf '[upgrade-dashboard] ERROR: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
(( $# == 0 )) || die "usage: $0 [--dry-run]"
(( EUID == 0 )) || die "run as root"

[[ -f "$ENV_FILE" ]] || die "missing local environment file: $ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || die "missing local compose file: $COMPOSE_FILE"
command -v awk >/dev/null 2>&1 || die "awk is required"
command -v grep >/dev/null 2>&1 || die "grep is required"

env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE"
}

require_env() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  [[ -n "$value" ]] || die "$key is missing or empty in $ENV_FILE"
}

require_min_env() {
  local key="$1"
  local minimum="$2"
  local value
  value="$(env_value "$key")"
  [[ ${#value} -ge "$minimum" ]] || die "$key is shorter than $minimum characters"
}

for key in \
  GROUPROXY_ENVIRONMENT \
  GROUPROXY_MONGODB_URL \
  GROUPROXY_MONGODB_DATABASE \
  GROUPROXY_HOST \
  GROUPROXY_PORT \
  GROUPROXY_BACKEND_PUBLIC_URL \
  GROUPROXY_BUNDLE_HMAC_SECRET \
  GROUPROXY_ADMIN_USERNAME \
  GROUPROXY_ADMIN_PASSWORD \
  GROUPROXY_MANAGEMENT_TOKEN \
  GROUPROXY_ALLOW_INSECURE_AGENT_HTTP \
  GROUPROXY_GQUAN_DELIVERY_MODE; do
  require_env "$key"
done
require_min_env GROUPROXY_BUNDLE_HMAC_SECRET 32
require_min_env GROUPROXY_ADMIN_PASSWORD 12
require_min_env GROUPROXY_MANAGEMENT_TOKEN 32

[[ "$(env_value GROUPROXY_ENVIRONMENT)" == "production" ]] || \
  die "GROUPROXY_ENVIRONMENT must be production"
[[ "$(env_value GROUPROXY_HOST)" == "0.0.0.0" ]] || \
  die "GROUPROXY_HOST must be 0.0.0.0 so remote monitors can connect"
[[ "$(env_value GROUPROXY_PORT)" == "8000" ]] || \
  die "GROUPROXY_PORT must be 8000 for the current monitor topology"
[[ "$(env_value GROUPROXY_ALLOW_INSECURE_AGENT_HTTP)" == "true" ]] || \
  die "current HTTP agent deployment requires GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true"
if [[ "$(env_value GROUPROXY_GQUAN_DELIVERY_MODE)" == "app" ]]; then
  require_env GROUPROXY_GQUAN_APP_TOKEN
fi

ensure_docker() {
  local config_changed=0
  local daemon_file=/etc/docker/daemon.json
  local mirrors_json='["https://docker.m.daocloud.io","https://dockerproxy.com","https://docker.mirrors.sjtug.sjtu.edu.cn","https://docker.nju.edu.cn"]'

  if ! command -v docker >/dev/null 2>&1; then
    (( DRY_RUN == 1 )) && {
      log "Docker is missing; a real run would install docker.io and Compose v2"
      return
    }
    command -v apt-get >/dev/null 2>&1 || die "Docker is missing and apt-get is unavailable"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y docker.io || die "unable to install docker.io"
  fi

  if ! docker compose version >/dev/null 2>&1; then
    (( DRY_RUN == 1 )) && {
      log "Docker Compose v2 is missing; a real run would install it"
      return
    }
    command -v apt-get >/dev/null 2>&1 || die "Docker Compose v2 is missing and apt-get is unavailable"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y docker-compose-v2 >/dev/null 2>&1 || \
      apt-get install -y docker-compose-plugin >/dev/null 2>&1 || \
      die "unable to install Docker Compose v2"
  fi

  # Preserve existing daemon settings and add the supplied mirrors only when
  # the current configuration does not already provide one.
  if [[ ! -e "$daemon_file" ]]; then
    (( DRY_RUN == 1 )) && {
      log "Docker has no daemon.json; a real install would add the configured registry mirrors"
    } || {
      install -d -m 0755 /etc/docker
      local temp_file
      temp_file="$(mktemp /etc/docker/daemon.json.XXXXXX)"
      printf '{\n  "registry-mirrors": %s\n}\n' "$mirrors_json" > "$temp_file"
      chmod 0644 "$temp_file"
      mv -f "$temp_file" /etc/docker/daemon.json
      config_changed=1
    }
  else
    if ! command -v jq >/dev/null 2>&1; then
      (( DRY_RUN == 1 )) && {
        log "jq is missing; a real run would install jq to merge Docker registry mirrors"
      } || {
        command -v apt-get >/dev/null 2>&1 || die "jq is required to preserve the existing daemon.json"
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y jq >/dev/null 2>&1 || die "unable to install jq"
      }
    fi
    if ! jq -e 'type == "object"' "$daemon_file" >/dev/null 2>&1; then
      die "Docker daemon.json is not a valid JSON object: $daemon_file"
    fi
    if ! jq -e '(.["registry-mirrors"]? // []) | type == "array" and length > 0' "$daemon_file" >/dev/null 2>&1; then
      (( DRY_RUN == 1 )) && {
        log "Docker daemon.json has no registry mirrors; a real run would merge the configured mirrors and preserve existing settings"
      } || {
        jq -e '(.["registry-mirrors"]? // []) | type == "array"' "$daemon_file" >/dev/null || \
          die "Docker daemon.json registry-mirrors must be an array"
        local temp_file
        temp_file="$(mktemp /etc/docker/daemon.json.XXXXXX)"
        if ! jq --argjson mirrors "$mirrors_json" \
          '.["registry-mirrors"] = (((.["registry-mirrors"] // []) + $mirrors) | unique)' \
          "$daemon_file" > "$temp_file"; then
          rm -f -- "$temp_file"
          die "unable to merge registry mirrors into $daemon_file"
        fi
        chmod 0644 "$temp_file"
        local backup_file
        backup_file="${daemon_file}.before-grouproxy-$(date -u +%Y%m%dT%H%M%SZ)"
        cp -p -- "$daemon_file" "$backup_file"
        mv -f "$temp_file" "$daemon_file"
        config_changed=1
      }
    fi
  fi

  (( DRY_RUN == 1 )) && return
  if (( config_changed == 1 )); then
    systemctl enable docker >/dev/null
    systemctl restart docker
  else
    systemctl enable --now docker
  fi
  docker info >/dev/null 2>&1 || die "Docker daemon is not ready"
}

compose=(
  docker compose
  --project-directory "$SOURCE_DIR"
  --project-name "$PROJECT_NAME"
  --file "$COMPOSE_FILE"
)

export GROUPROXY_BACKEND_ENV_FILE="$ENV_FILE"
export GROUPROXY_BACKEND_API_URL="$FRONTEND_BACKEND_URL"
export GROUPROXY_FRONTEND_PORT=80

on_exit() {
  local status=$?
  trap - EXIT
  if (( status != 0 && ROLLBACK_NEEDED == 1 )); then
    log "Docker deployment failed; removing the new containers"
    "${compose[@]}" down >/dev/null 2>&1 || true
    if (( NATIVE_BACKEND_ACTIVE == 1 )); then
      systemctl enable --now grouproxy-backend.service >/dev/null 2>&1 || true
    fi
    if (( NATIVE_DASHBOARD_ACTIVE == 1 )); then
      systemctl enable --now grouproxy-dashboard.service >/dev/null 2>&1 || true
    fi
  fi
  exit "$status"
}
trap on_exit EXIT

wait_for_health() {
  local backend_id frontend_id backend_health frontend_health backend_response
  for _ in $(seq 1 90); do
    backend_id="$("${compose[@]}" ps -q backend 2>/dev/null || true)"
    frontend_id="$("${compose[@]}" ps -q frontend 2>/dev/null || true)"
    backend_health=""
    frontend_health=""
    if [[ -n "$backend_id" ]]; then
      backend_health="$(docker inspect --format '{{.State.Health.Status}}' "$backend_id" 2>/dev/null || true)"
    fi
    if [[ -n "$frontend_id" ]]; then
      frontend_health="$(docker inspect --format '{{.State.Health.Status}}' "$frontend_id" 2>/dev/null || true)"
    fi
    if [[ "$backend_health" == "healthy" && "$frontend_health" == "healthy" ]]; then
      if backend_response="$(curl -fsS --max-time 5 http://127.0.0.1:8000/healthz 2>/dev/null)" && \
        grep -q '"version":"0.6.0"' <<<"$backend_response" && \
        curl -fsS --max-time 5 http://127.0.0.1/healthz >/dev/null && \
        curl -fsS --max-time 5 http://127.0.0.1/login >/dev/null; then
        return 0
      fi
    fi
    sleep 2
  done
  "${compose[@]}" ps >&2 || true
  die "Docker Dashboard/Backend health check timed out"
}

verify_runtime_environment() {
  local backend_id frontend_id backend_env frontend_env
  backend_id="$("${compose[@]}" ps -q backend)"
  frontend_id="$("${compose[@]}" ps -q frontend)"
  [[ -n "$backend_id" && -n "$frontend_id" ]] || die "expected Compose containers are missing"
  backend_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$backend_id")"
  frontend_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$frontend_id")"
  for key in \
    GROUPROXY_ENVIRONMENT \
    GROUPROXY_MONGODB_URL \
    GROUPROXY_MONGODB_DATABASE \
    GROUPROXY_BACKEND_PUBLIC_URL \
    GROUPROXY_BUNDLE_HMAC_SECRET \
    GROUPROXY_ADMIN_USERNAME \
    GROUPROXY_ADMIN_PASSWORD \
    GROUPROXY_MANAGEMENT_TOKEN \
    GROUPROXY_ALLOW_INSECURE_AGENT_HTTP \
    GROUPROXY_GQUAN_DELIVERY_MODE; do
    grep -q "^${key}=" <<<"$backend_env" || die "Backend container is missing $key"
  done
  grep -q '^NODE_ENV=production$' <<<"$frontend_env" || die "Frontend container is not in production mode"
  grep -q '^PORT=80$' <<<"$frontend_env" || die "Frontend container is not listening on port 80"
}

ensure_docker

if (( DRY_RUN == 1 )); then
  log "validated $ENV_FILE"
  log "validated $COMPOSE_FILE"
  log "would build local backend/frontend images with GROUPROXY_BACKEND_ENV_FILE=$ENV_FILE"
  log "would use frontend rewrite target $FRONTEND_BACKEND_URL"
  log "dry-run complete; no service or container was changed"
  exit 0
fi

"${compose[@]}" config --quiet
log "building backend and frontend images from $SOURCE_DIR"
"${compose[@]}" build --pull backend frontend

if systemctl is-active --quiet grouproxy-backend.service; then
  NATIVE_BACKEND_ACTIVE=1
fi
if systemctl is-active --quiet grouproxy-dashboard.service; then
  NATIVE_DASHBOARD_ACTIVE=1
fi
ROLLBACK_NEEDED=1

# Free :80 and :8000 only after the local image build succeeds.
if (( NATIVE_DASHBOARD_ACTIVE == 1 )); then
  systemctl stop grouproxy-dashboard.service
fi
if (( NATIVE_BACKEND_ACTIVE == 1 )); then
  systemctl stop grouproxy-backend.service
fi

log "starting Docker Compose services"
"${compose[@]}" up -d --force-recreate backend frontend
wait_for_health
verify_runtime_environment

# The native units must not race the Docker services after reboot.
if ! systemctl disable grouproxy-dashboard.service grouproxy-backend.service >/dev/null 2>&1; then
  die "unable to disable the native Dashboard/Backend services"
fi
ROLLBACK_NEEDED=0
log "Dashboard and Backend are running in Docker"
"${compose[@]}" ps
