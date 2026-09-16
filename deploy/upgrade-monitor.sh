#!/usr/bin/env bash
set -Eeuo pipefail

# This script is installed as /opt/one_proxy/upgrade-monitor.sh. It uses the
# checked-in Linux amd64 monitor artifact and the node's existing local config.
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$ROOT_DIR/src"
ENV_FILE="$ROOT_DIR/etc/backend.env"
CONFIG_FILE="$ROOT_DIR/etc/monitor.yaml"
CURRENT_BINARY="$ROOT_DIR/monitor"
ARTIFACT="$SOURCE_DIR/monitor/dist/grouproxy-monitor-linux-amd64"
CHECKSUMS="$SOURCE_DIR/monitor/dist/SHA256SUMS"
SERVICE="grouproxy-monitor.service"
DRY_RUN=0

log() {
  printf '[upgrade-monitor] %s\n' "$*"
}

die() {
  printf '[upgrade-monitor] ERROR: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
(( $# == 0 )) || die "usage: $0 [--dry-run]"
(( EUID == 0 )) || die "run as root"

[[ -f "$ENV_FILE" ]] || die "missing local environment file: $ENV_FILE"
[[ -f "$CONFIG_FILE" ]] || die "missing monitor config: $CONFIG_FILE"
[[ -f "$ARTIFACT" ]] || die "missing checked-in monitor artifact: $ARTIFACT"
[[ -f "$CHECKSUMS" ]] || die "missing monitor checksum file: $CHECKSUMS"
[[ -x "$ROOT_DIR/sing-box" ]] || die "missing executable sing-box binary: $ROOT_DIR/sing-box"
[[ -x "$CURRENT_BINARY" ]] || die "missing executable current monitor: $CURRENT_BINARY"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
command -v date >/dev/null 2>&1 || die "date is required"

env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE"
}

yaml_value() {
  local key="$1"
  sed -n "s/^${key}:[[:space:]]*//p" "$CONFIG_FILE" \
    | head -n 1 \
    | sed -E "s/^[\"']|[\"']$//g"
}

bundle_secret="$(env_value GROUPROXY_BUNDLE_HMAC_SECRET)"
backend_url="$(env_value GROUPROXY_BACKEND_PUBLIC_URL)"
backend_port="$(env_value GROUPROXY_PORT)"
backend_port="${backend_port:-8000}"
local_backend_url="http://127.0.0.1:${backend_port}"
monitor_backend_url="$(yaml_value backend_url)"
[[ ${#bundle_secret} -ge 32 ]] || die "GROUPROXY_BUNDLE_HMAC_SECRET is too short"
[[ -n "$backend_url" ]] || die "GROUPROXY_BACKEND_PUBLIC_URL is missing or empty"
[[ "$(env_value GROUPROXY_ENVIRONMENT)" == "production" ]] || \
  die "GROUPROXY_ENVIRONMENT must be production"
[[ "$backend_port" == "8000" ]] || \
  die "GROUPROXY_PORT must be 8000 for the current monitor topology"
[[ "$(env_value GROUPROXY_ALLOW_INSECURE_AGENT_HTTP)" == "true" ]] || \
  die "current HTTP agent deployment requires GROUPROXY_ALLOW_INSECURE_AGENT_HTTP=true"
[[ "$(yaml_value node_id)" == "beijing" ]] || \
  die "monitor config is not the Beijing node"
[[ "$monitor_backend_url" == "$backend_url" || "$monitor_backend_url" == "$local_backend_url" ]] || \
  die "monitor backend_url must match the public Backend URL or local ${local_backend_url}"
[[ "$(yaml_value hmac_secret)" == "$bundle_secret" ]] || \
  die "monitor hmac_secret does not match GROUPROXY_BUNDLE_HMAC_SECRET"
[[ "$(yaml_value listen_port)" == "1080" ]] || die "monitor listen_port must be 1080"

verify_artifact() {
  (cd "$SOURCE_DIR/monitor" && sha256sum -c dist/SHA256SUMS)
}

run_as_grouproxy() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u grouproxy -- "$@"
  else
    command -v sudo >/dev/null 2>&1 || die "runuser or sudo is required"
    sudo -u grouproxy -- "$@"
  fi
}

verify_artifact

candidate="$ROOT_DIR/.monitor.new.$$"
backup="$ROOT_DIR/monitor.previous.$(date -u +%Y%m%dT%H%M%SZ)"
was_active=0
backup_created=0
rollback_needed=0
cleanup() {
  rm -f -- "$candidate"
}

on_exit() {
  local status=$?
  trap - EXIT
  if (( status != 0 && rollback_needed == 1 )); then
    log "monitor upgrade failed; restoring the previous binary"
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    if (( backup_created == 1 )); then
      rm -f -- "$CURRENT_BINARY"
      if ! mv -- "$backup" "$CURRENT_BINARY" || ! chown grouproxy:grouproxy "$CURRENT_BINARY"; then
        log "ERROR: unable to restore $backup" >&2
      fi
    fi
    if (( was_active == 1 )); then
      systemctl start "$SERVICE" >/dev/null 2>&1 || true
    fi
  fi
  cleanup
  exit "$status"
}
trap on_exit EXIT

install -m 0755 "$ARTIFACT" "$candidate"
run_as_grouproxy "$candidate" -config "$CONFIG_FILE" -validate

if (( DRY_RUN == 1 )); then
  log "validated $ENV_FILE, $CONFIG_FILE, monitor artifact, and sing-box integrity"
  log "would replace $CURRENT_BINARY and restart $SERVICE"
  log "dry-run complete; no binary or service was changed"
  exit 0
fi

if systemctl is-active --quiet "$SERVICE"; then
  was_active=1
fi

rollback_needed=1
if (( was_active == 1 )); then
  systemctl stop "$SERVICE"
fi

mv -- "$CURRENT_BINARY" "$backup"
backup_created=1
mv -- "$candidate" "$CURRENT_BINARY"
chown grouproxy:grouproxy "$CURRENT_BINARY"

if (( was_active == 1 )); then
  if ! systemctl start "$SERVICE"; then
    die "monitor restart failed"
  fi
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE"; then
      log "monitor service is active"
      rollback_needed=0
      exit 0
    fi
    sleep 1
  done
  die "monitor service did not become active"
fi

rollback_needed=0
log "monitor binary updated; service was inactive and remains inactive"
