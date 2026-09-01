#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_PORT="${GROUPROXY_TEST_PUBLIC_PORT:-80}"
SINGBOX_PORT="${GROUPROXY_TEST_PROXY_LISTEN_PORT:-18080}"
DASHBOARD_HOST="${GROUPROXY_TEST_PROXY_ACCESS_FQDN:-test-proxy.1oa.com.cn}"

nginx -t >/dev/null
if nft list table inet grouproxy >/dev/null 2>&1; then
  printf 'The co-located test profile must not apply a packet-level :80 ACL that also gates /dashboard.\n' >&2
  exit 1
fi
ss -lnt | awk -v port=":${PUBLIC_PORT}" '$1 == "LISTEN" && $4 ~ port "$" { found=1 } END { exit !found }'
ss -lnt | awk -v address="127.0.0.1:${SINGBOX_PORT}" '$1 == "LISTEN" && $4 == address { found=1 } END { exit !found }'
if ss -lnt | awk -v address="0.0.0.0:${SINGBOX_PORT}" '$1 == "LISTEN" && $4 == address { found=1 } END { exit !found }'; then
  printf 'sing-box internal port %s is exposed on 0.0.0.0\n' "$SINGBOX_PORT" >&2
  exit 1
fi

check_dashboard() {
  local url="$1"
  for _ in $(seq 1 10); do
    if curl -fsS --noproxy '*' --max-time 10 \
      --resolve "${DASHBOARD_HOST}:${PUBLIC_PORT}:127.0.0.1" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

# A reload is graceful, so briefly retry while old workers finish existing
# sessions and the new stream script takes ownership of the listener.
check_dashboard "http://${DASHBOARD_HOST}:${PUBLIC_PORT}/dashboard"
check_dashboard "http://${DASHBOARD_HOST}:${PUBLIC_PORT}/dashboard/backend-api/readyz"

raw_status() {
  local request="$1"
  # nc returns a non-zero status when the upstream closes immediately after
  # the response; the HTTP status line is the assertion we need here.
  printf '%b' "$request" | nc -w 5 127.0.0.1 "$PUBLIC_PORT" 2>/dev/null \
    | sed -n '1s/^HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' || true
}

absolute_dashboard_status=""
for _ in $(seq 1 10); do
  absolute_dashboard_status="$(raw_status "GET http://${DASHBOARD_HOST}:${PUBLIC_PORT}/dashboard?request_target=absolute HTTP/1.1\\r\\nHost: ${DASHBOARD_HOST}:${PUBLIC_PORT}\\r\\nConnection: close\\r\\n\\r\\n")"
  [[ "$absolute_dashboard_status" == "200" ]] && break
  sleep 0.2
done
[[ "$absolute_dashboard_status" == "200" ]] || {
  printf 'absolute-form dashboard request returned HTTP %s\n' "${absolute_dashboard_status:-none}" >&2
  exit 1
}

stream_log_before="$(wc -l < /var/log/nginx/grouproxy-stream-access.log 2>/dev/null || printf '0')"
raw_status 'GET http://127.0.0.1:1/dashboard HTTP/1.1\r\nHost: 127.0.0.1:1\r\nConnection: close\r\n\r\n' >/dev/null
foreign_absolute_route=""
for _ in $(seq 1 10); do
  foreign_absolute_route="$(tail -n +$((stream_log_before + 1)) /var/log/nginx/grouproxy-stream-access.log 2>/dev/null | tail -n 1 | sed -n 's/.*route=\([^ ]*\).*/\1/p')"
  [[ "$foreign_absolute_route" == "proxy" ]] && break
  sleep 0.2
done
[[ "$foreign_absolute_route" == "proxy" ]] || {
  printf 'foreign absolute-form request did not stay on the proxy route (route=%s)\n' "${foreign_absolute_route:-none}" >&2
  exit 1
}

if [[ -n "${GROUPROXY_TEST_PROXY_USERNAME:-}" && -n "${GROUPROXY_TEST_PROXY_PASSWORD:-}" ]]; then
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
    --proxy "http://127.0.0.1:${PUBLIC_PORT}" \
    --proxy-user "${GROUPROXY_TEST_PROXY_USERNAME}:${GROUPROXY_TEST_PROXY_PASSWORD}" \
    https://www.google.com/ncr)"
  [[ "$status" =~ ^(2|3) ]] || { printf 'Google proxy check returned HTTP %s\n' "$status" >&2; exit 1; }
  printf 'Entrypoint verified, including one low-traffic Google request (HTTP %s).\n' "$status"
else
  printf 'Entrypoint and dashboard verified. Proxy egress check skipped because test proxy credentials were not supplied.\n'
fi
