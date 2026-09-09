#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"
ENV_FILE="$TESTENV_DIR/backend.env"

[[ -f "$ENV_FILE" ]] || {
  printf 'Run scripts/testenv-up.sh first.\n' >&2
  exit 1
}

set -a
source "$ENV_FILE"
set +a

BACKEND_URL="http://127.0.0.1:${GROUPROXY_PORT:-8000}"
AUTH_HEADER="Authorization: Bearer ${GROUPROXY_MANAGEMENT_TOKEN}"

# The data plane is network-ACL-only. Its connection contract is fixed at
# port 1080 and has no HTTP Basic credential endpoint to exercise.
access_config="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/access/config")"
jq -e '.protocol == "http-connect" and (.port == 1080)' <<<"$access_config" >/dev/null

backup_response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/backups" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: phase4-backup-rehearsal-$(date +%s%N)" \
  -d '{"scope":"control_plane"}')"
backup_id="$(jq -r '.backup.backup_id' <<<"$backup_response")"
backup_task_id="$(jq -r '.task.task_id' <<<"$backup_response")"
[[ -n "$backup_id" && "$backup_id" != "null" && -n "$backup_task_id" && "$backup_task_id" != "null" ]]

backup_task=""
for _ in $(seq 1 90); do
  backup_task="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/tasks/${backup_task_id}")"
  if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$backup_task" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100 and .result.collections > 0' <<<"$backup_task" >/dev/null

backup_record="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/backups" | jq -e --arg backup "$backup_id" '.[] | select(.backup_id == $backup)')"
jq -e '.origin == "manual" and .status == "verified" and .encrypted == true and (.storage_ref | length > 0) and .size_bytes > 0 and .verified_at != null and .last_rehearsed_at == null' <<<"$backup_record" >/dev/null
backup_artifact="$(jq -r '.storage_ref' <<<"$backup_record")"
[[ -f "$TESTENV_DIR/backups/${backup_artifact}" ]]
[[ "$(stat -c '%a' "$TESTENV_DIR/backups/${backup_artifact}")" == "600" ]]

rehearsal_response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/backups/${backup_id}/restore" \
  -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: phase4-backup-rehearsal-check-$(date +%s%N)" \
  -d '{"confirm":false}')"
rehearsal_task_id="$(jq -r '.task.task_id' <<<"$rehearsal_response")"
[[ -n "$rehearsal_task_id" && "$rehearsal_task_id" != "null" ]]

rehearsal_task=""
for _ in $(seq 1 90); do
  rehearsal_task="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/tasks/${rehearsal_task_id}")"
  if jq -e '.status == "succeeded" and .stage == "succeeded" and .progress == 100' <<<"$rehearsal_task" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e '.status == "succeeded" and .result.mode == "rehearsal" and .result.applied == 0 and .result.collections > 0' <<<"$rehearsal_task" >/dev/null

backup_record="$(curl -fsS -H "$AUTH_HEADER" "$BACKEND_URL/api/v1/backups" | jq -e --arg backup "$backup_id" '.[] | select(.backup_id == $backup)')"
jq -e '.origin == "manual" and .status == "rehearsed" and .verified_at != null and .last_rehearsed_at != null and .encrypted == true' <<<"$backup_record" >/dev/null

printf 'Phase 4 encrypted backup rehearsal validation passed.\n'
