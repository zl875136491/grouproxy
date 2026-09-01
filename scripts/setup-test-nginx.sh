#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_PORT="${GROUPROXY_TEST_PUBLIC_PORT:-80}"
SINGBOX_PORT="${GROUPROXY_TEST_PROXY_LISTEN_PORT:-18080}"
DASHBOARD_HTTP_PORT="${GROUPROXY_TEST_DASHBOARD_HTTP_PORT:-18082}"
BACKEND_PORT="${GROUPROXY_TEST_BACKEND_PORT:-8000}"
FRONTEND_PORT="${GROUPROXY_TEST_FRONTEND_PORT:-3000}"
DASHBOARD_HOST="${GROUPROXY_TEST_PROXY_ACCESS_FQDN:-test-proxy.1oa.com.cn}"
PROXY_ALLOW_CIDRS="${GROUPROXY_TEST_PROXY_ALLOW_CIDRS:-10.32.12.0/24,127.0.0.1/32}"

[[ "$EUID" == "0" ]] || { printf 'Root is required to install Nginx configuration and bind port %s.\n' "$PUBLIC_PORT" >&2; exit 1; }
[[ "$DASHBOARD_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || { printf 'Invalid dashboard host: %s\n' "$DASHBOARD_HOST" >&2; exit 1; }
[[ "$PROXY_ALLOW_CIDRS" =~ ^[0-9.,/[:space:]]+$ ]] || { printf 'Invalid IPv4 proxy CIDR list.\n' >&2; exit 1; }
for port in "$PUBLIC_PORT" "$SINGBOX_PORT" "$DASHBOARD_HTTP_PORT" "$BACKEND_PORT" "$FRONTEND_PORT"; do
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || {
    printf 'Invalid TCP port: %s\n' "$port" >&2
    exit 1
  }
done

packages=(nginx libnginx-mod-stream libnginx-mod-stream-js)
missing=()
for package in "${packages[@]}"; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | rg -q '^install ok installed$' || missing+=("$package")
done
if ((${#missing[@]})); then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
fi

runtime_dir="$(mktemp -d /tmp/grouproxy-nginx.XXXXXX)"
cleanup() {
  rm -rf -- "$runtime_dir"
}
trap cleanup EXIT

render() {
  local source="$1" target="$2"
  sed \
    -e "s|@@PUBLIC_PORT@@|$PUBLIC_PORT|g" \
    -e "s|@@SINGBOX_PORT@@|$SINGBOX_PORT|g" \
    -e "s|@@DASHBOARD_HTTP_PORT@@|$DASHBOARD_HTTP_PORT|g" \
    -e "s|@@BACKEND_PORT@@|$BACKEND_PORT|g" \
    -e "s|@@FRONTEND_PORT@@|$FRONTEND_PORT|g" \
    -e "s|@@DASHBOARD_HOST@@|$DASHBOARD_HOST|g" \
    -e "s|@@PROXY_ALLOW_CIDRS@@|$PROXY_ALLOW_CIDRS|g" \
    "$source" > "$target"
}

render "$ROOT_DIR/deploy/nginx/grouproxy-entrypoint.conf.template" "$runtime_dir/grouproxy-entrypoint.conf"
render "$ROOT_DIR/deploy/nginx/grouproxy-dashboard.conf.template" "$runtime_dir/grouproxy-dashboard.conf"
render "$ROOT_DIR/deploy/nginx/grouproxy-stream.js" "$runtime_dir/grouproxy-stream.js"

install -d -m 0755 /etc/nginx/njs
install -m 0644 "$runtime_dir/grouproxy-stream.js" /etc/nginx/njs/grouproxy-stream.js
install -m 0644 "$runtime_dir/grouproxy-entrypoint.conf" /etc/nginx/modules-enabled/99-grouproxy-entrypoint.conf
install -m 0644 "$runtime_dir/grouproxy-dashboard.conf" /etc/nginx/conf.d/grouproxy-dashboard.conf

# The package default vhost also claims :80 in the HTTP layer. The source file
# remains in sites-available, so disabling the symlink is reversible.
if [[ -L /etc/nginx/sites-enabled/default ]]; then
  unlink /etc/nginx/sites-enabled/default
fi

# A packet-level dport 80 rule would also gate /dashboard. The co-located test
# profile delegates proxy-only source filtering to stream js_access. Never
# delete or rewrite an existing firewall table here; an operator must decide
# how to migrate an already-applied proxy policy without touching other ports.
if nft list table inet grouproxy >/dev/null 2>&1; then
  printf 'Existing inet grouproxy table detected; leaving it unchanged because a packet-level :80 ACL also gates /dashboard.\n' >&2
  printf 'Set monitor firewall_mode=dry-run for this co-located profile or migrate the table explicitly before retrying.\n' >&2
  exit 1
fi

nginx -t
systemctl enable nginx >/dev/null
if systemctl is-active --quiet nginx; then
  systemctl reload nginx
else
  systemctl start nginx
fi

printf 'Nginx entrypoint active: http://%s:%s/dashboard -> frontend 127.0.0.1:%s, API 127.0.0.1:%s, proxy 127.0.0.1:%s\n' \
  "$DASHBOARD_HOST" "$PUBLIC_PORT" "$FRONTEND_PORT" "$BACKEND_PORT" "$SINGBOX_PORT"
