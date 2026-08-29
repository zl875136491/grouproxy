#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTENV_DIR="${GROUPROXY_TESTENV_DIR:-$ROOT_DIR/testenv}"

stop_pid_file() {
  local path="$1" pid pgid
  [[ -f "$path" ]] || return 0
  pid="$(<"$path")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  # testenv-up launches services in their own session. The pid file points
  # at the session leader, while npm and sing-box may be child processes;
  # terminate the whole process group so a stop/reset cannot leave listeners.
  # ps exits nonzero after a previously stopped leader; tolerate that so the
  # remaining child process group can still be terminated from its saved PID.
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  [[ "$pgid" =~ ^[0-9]+$ ]] || pgid="$pid"
  if [[ "$pgid" =~ ^[0-9]+$ ]] && (( pgid > 1 )); then
    kill -TERM -- "-$pgid" 2>/dev/null || true
    for _ in $(seq 1 40); do
      kill -0 -- "-$pgid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL -- "-$pgid" 2>/dev/null || true
  elif kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
  rm -f "$path"
}

stop_pid_file "$TESTENV_DIR/frontend.pid"
stop_pid_file "$TESTENV_DIR/backend.pid"
for path in "$TESTENV_DIR"/monitor-*/monitor.pid; do
  stop_pid_file "$path"
done

printf 'Stopped Grouproxy test processes. Runtime data remains in %s for inspection.\n' "$TESTENV_DIR"
