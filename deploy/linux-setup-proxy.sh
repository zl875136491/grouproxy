#!/usr/bin/env bash
set -Eeuo pipefail

# This helper configures the HTTP CONNECT path only. It never installs a
# certificate or enables an HTTPS proxy listener.
PROXY_HOST="${GROUPROXY_PROXY_HOST:-proxy.corp.internal}"
PROXY_PORT="${GROUPROXY_PROXY_PORT:-80}"
NO_PROXY_VALUE="${NO_PROXY:-localhost,127.0.0.1,.corp.internal}"

export http_proxy="http://${PROXY_HOST}:${PROXY_PORT}"
export https_proxy="http://${PROXY_HOST}:${PROXY_PORT}"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export no_proxy="$NO_PROXY_VALUE"
export NO_PROXY="$NO_PROXY_VALUE"

if [[ "${1:-}" == "--persist" ]]; then
  profile_file="${HOME}/.config/grouproxy/proxy.env"
  mkdir -p "$(dirname "$profile_file")"
  umask 077
  {
    printf 'export http_proxy=%q\n' "$http_proxy"
    printf 'export https_proxy=%q\n' "$https_proxy"
    printf 'export HTTP_PROXY=%q\n' "$HTTP_PROXY"
    printf 'export HTTPS_PROXY=%q\n' "$HTTPS_PROXY"
    printf 'export no_proxy=%q\n' "$no_proxy"
    printf 'export NO_PROXY=%q\n' "$NO_PROXY"
  } > "$profile_file"
  printf 'Saved shell proxy environment to %s\n' "$profile_file"
fi

printf 'HTTP CONNECT proxy configured at %s:%s\n' "$PROXY_HOST" "$PROXY_PORT"
printf 'HTTPS proxy transport and CA installation are intentionally disabled.\n'
