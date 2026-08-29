#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
ENV_FILE="$TESTENV_DIR/backend.env"
[[ -f "$ENV_FILE" ]] || { echo 'Run scripts/testenv-up.sh first' >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a

[[ "$GROUPROXY_GQUAN_DELIVERY_MODE" == "stub" ]] || {
  echo 'Automated auth verification requires a test environment started with GROUPROXY_TEST_GQUAN_DELIVERY_MODE=stub.' >&2
  exit 1
}

BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
AUTH_CODE="${GROUPROXY_GQUAN_TEST_CODE:?Missing GROUPROXY_GQUAN_TEST_CODE}"
ITCODE="phase3-auth-$(openssl rand -hex 5)"
INITIAL_PASSWORD="phase3-auth-password"
UPDATED_PASSWORD="phase3-updated-password"

request_code() {
  local purpose="$1"
  curl -fsS -X POST "$BACKEND_URL/api/v1/auth/verification-codes" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg itcode "$ITCODE" --arg purpose "$purpose" '{itcode:$itcode,purpose:$purpose}')" \
    | jq -r '.challenge_id'
}

register_challenge="$(request_code register)"
[[ -n "$register_challenge" && "$register_challenge" != "null" ]]
curl -fsS -X POST "$BACKEND_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg itcode "$ITCODE" --arg password "$INITIAL_PASSWORD" --arg challenge "$register_challenge" --arg code "$AUTH_CODE" '{itcode:$itcode,password:$password,challenge_id:$challenge,verification_code:$code}')" \
  | jq -e '.status == "ok"' >/dev/null

password_login="$(curl -fsS -X POST "$BACKEND_URL/api/v1/auth/login" -H 'Content-Type: application/json' -d "$(jq -nc --arg itcode "$ITCODE" --arg password "$INITIAL_PASSWORD" '{itcode:$itcode,password:$password}')")"
password_token="$(jq -r '.access_token' <<<"$password_login")"
[[ -n "$password_token" && "$password_token" != "null" ]]
curl -fsS -H "Authorization: Bearer $password_token" "$BACKEND_URL/api/v1/sites" | jq -e 'type == "array"' >/dev/null

gquan_challenge="$(request_code gquan_login)"
gquan_login="$(curl -fsS -X POST "$BACKEND_URL/api/v1/auth/gquan/login" -H 'Content-Type: application/json' -d "$(jq -nc --arg itcode "$ITCODE" --arg challenge "$gquan_challenge" --arg code "$AUTH_CODE" '{itcode:$itcode,challenge_id:$challenge,verification_code:$code}')")"
gquan_token="$(jq -r '.access_token' <<<"$gquan_login")"
[[ -n "$gquan_token" && "$gquan_token" != "null" ]]

change_challenge="$(request_code password_change)"
curl -fsS -X POST "$BACKEND_URL/api/v1/auth/password/change" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg itcode "$ITCODE" --arg password "$UPDATED_PASSWORD" --arg challenge "$change_challenge" --arg code "$AUTH_CODE" '{itcode:$itcode,password:$password,challenge_id:$challenge,verification_code:$code}')" \
  | jq -e '.status == "ok"' >/dev/null

revoked_session_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $gquan_token" "$BACKEND_URL/api/v1/sites")"
[[ "$revoked_session_status" == "401" ]]

old_login_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BACKEND_URL/api/v1/auth/login" -H 'Content-Type: application/json' -d "$(jq -nc --arg itcode "$ITCODE" --arg password "$INITIAL_PASSWORD" '{itcode:$itcode,password:$password}')")"
[[ "$old_login_status" == "401" ]]
updated_login="$(curl -fsS -X POST "$BACKEND_URL/api/v1/auth/login" -H 'Content-Type: application/json' -d "$(jq -nc --arg itcode "$ITCODE" --arg password "$UPDATED_PASSWORD" '{itcode:$itcode,password:$password}')")"
updated_token="$(jq -r '.access_token' <<<"$updated_login")"
[[ -n "$updated_token" && "$updated_token" != "null" ]]
curl -fsS -X POST "$BACKEND_URL/api/v1/auth/logout" -H "Authorization: Bearer $updated_token" | jq -e '.status == "ok"' >/dev/null

echo "Authentication validation passed for test account $ITCODE."
