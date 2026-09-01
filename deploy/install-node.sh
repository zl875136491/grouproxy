#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_USER="${NUC_SSH_USER:-}"
SSH_KEY="${NUC_SSH_KEY:-}"
TARGET_HOST="${1:-}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$TARGET_HOST" || -z "$SSH_USER" ]]; then
  printf 'Usage: NUC_SSH_USER=... [NUC_SSH_KEY=/path/key] %s <host>\n' "$0" >&2
  exit 2
fi

SSH_ARGS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$SSH_KEY" ]]; then
  SSH_ARGS+=(-i "$SSH_KEY")
fi
REMOTE="${SSH_USER}@${TARGET_HOST}"
NODE_CONFIG_PATH=/opt/grouproxy/etc/monitor.yaml
MONITOR_BIN_PATH=/opt/grouproxy/bin/grouproxy-monitor

run_remote() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ ssh %q %q\n' "$REMOTE" "$*"
  else
    ssh "${SSH_ARGS[@]}" "$REMOTE" "$@"
  fi
}

printf 'Installing node artifacts on %s (control plane remains on codedev)\n' "$REMOTE"
run_remote "sudo groupadd --system grouproxy 2>/dev/null || true"
run_remote "id -u grouproxy >/dev/null 2>&1 || sudo useradd --system --gid grouproxy --home-dir /opt/grouproxy --shell /usr/sbin/nologin grouproxy"
run_remote "sudo install -d -m 0755 /opt/grouproxy/bin"
run_remote "sudo install -d -o grouproxy -g grouproxy -m 0750 /opt/grouproxy/etc /opt/grouproxy/var /opt/grouproxy/run"
if [[ "$DRY_RUN" != "1" ]]; then
  scp "${SSH_ARGS[@]}" "$ROOT_DIR/monitor/dist/grouproxy-monitor-linux-amd64" "$REMOTE:/tmp/grouproxy-monitor"
  scp "${SSH_ARGS[@]}" "$ROOT_DIR/singbox/sing-box" "$REMOTE:/tmp/sing-box"
  scp "${SSH_ARGS[@]}" "$ROOT_DIR/deploy/grouproxy-monitor.service" "$ROOT_DIR/deploy/sing-box.service" "$REMOTE:/tmp/"
  run_remote "sudo install -m 0755 /tmp/grouproxy-monitor /opt/grouproxy/bin/grouproxy-monitor"
  run_remote "sudo install -m 0755 /tmp/sing-box /opt/grouproxy/bin/sing-box"
  run_remote "sudo install -m 0644 /tmp/grouproxy-monitor.service /etc/systemd/system/grouproxy-monitor.service"
  run_remote "sudo install -m 0644 /tmp/sing-box.service /etc/systemd/system/sing-box.service"
  run_remote "sudo systemctl daemon-reload"
  # Validate the complete monitor configuration, including the token file,
  # before enabling the service. The validation path performs no network call
  # and does not apply a bundle or firewall policy.
  if run_remote "sudo -u grouproxy $MONITOR_BIN_PATH -config $NODE_CONFIG_PATH -validate"; then
    # monitor owns the child sing-box lifecycle. Enabling the standalone unit
    # too would create a second process and make the proxy port race at boot.
    run_remote "sudo systemctl enable --now grouproxy-monitor.service"
    printf 'Monitor configuration validated; grouproxy-monitor.service enabled and started.\n'
  else
    printf 'Node artifacts installed, but monitor.yaml or its token_file is missing/invalid; service was not enabled or started.\n' >&2
    printf 'Create %s and the referenced token as user grouproxy, then run: sudo systemctl enable --now grouproxy-monitor.service\n' "$NODE_CONFIG_PATH" >&2
    exit 1
  fi
else
  # Keep dry-run output representative of the real ordering without trying to
  # inspect files on the operator workstation.
  run_remote "sudo -u grouproxy $MONITOR_BIN_PATH -config $NODE_CONFIG_PATH -validate"
  run_remote "sudo systemctl enable --now grouproxy-monitor.service"
fi
if [[ "$DRY_RUN" == "1" ]]; then
  printf 'Dry-run complete. The remote monitor validation precedes service enablement.\n'
fi
