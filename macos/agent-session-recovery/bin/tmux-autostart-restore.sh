#!/bin/bash
set -u

: "${HOME:?HOME must be set by launchd or the calling shell}"
export PATH="${SESSION_RESTORE_PATH:-/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
RESTORE_SCRIPT="$HOME/.tmux/plugins/tmux-resurrect/scripts/restore.sh"
MOSHI_RECONCILER="$HOME/.tmux/reconcile-moshi-sessions.py"
RESURRECT_DIR="$HOME/.tmux/resurrect"
BOOTSTRAP_SESSION="autostart"
RESTORE_STATE_OPTION="@agent-session-boot-restore-state"
restore_state_claimed=0
server_pid=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

count_snapshot_panes() {
  awk -F '\t' '$1 == "pane" { count++ } END { print count + 0 }' "$1"
}

count_live_panes() {
  "$TMUX_BIN" list-panes -a -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' '
}

finish_restore_state() {
  status=$?
  if [ "$restore_state_claimed" -eq 1 ] && [ -n "$server_pid" ]; then
    if [ "$status" -eq 0 ]; then
      "$TMUX_BIN" set-option -g "$RESTORE_STATE_OPTION" "$server_pid:complete" 2>/dev/null || true
    else
      "$TMUX_BIN" set-option -g "$RESTORE_STATE_OPTION" "$server_pid:failed" 2>/dev/null || true
    fi
  fi
  exit "$status"
}

if [ ! -x "$TMUX_BIN" ]; then
  log "tmux binary missing: $TMUX_BIN"
  exit 1
fi

if ! "$TMUX_BIN" has-session >/dev/null 2>&1; then
  log "starting default tmux server"
  "$TMUX_BIN" new-session -d -s "$BOOTSTRAP_SESSION"
fi

if [ ! -x "$RESTORE_SCRIPT" ]; then
  log "restore script missing: $RESTORE_SCRIPT"
  exit 1
fi

last_file="$RESURRECT_DIR/last"
if [ ! -f "$last_file" ]; then
  log "no resurrect snapshot at $last_file; leaving bootstrap server running"
  exit 0
fi

expected_panes="$(count_snapshot_panes "$last_file")"
if [ "$expected_panes" -eq 0 ]; then
  log "refusing empty resurrect snapshot: $last_file"
  exit 1
fi

socket_path="$("$TMUX_BIN" display-message -p '#{socket_path}')"
server_pid="$("$TMUX_BIN" display-message -p '#{pid}')"
if [ -z "$socket_path" ] || ! printf '%s' "$server_pid" | grep -Eq '^[0-9]+$'; then
  log "could not identify the default tmux server"
  exit 1
fi

# RunAtLoad can be triggered again when the LaunchAgent is reinstalled. Restoring
# the same snapshot into an already restored server duplicates windows and sends
# the same per-pane Codex resume command twice. Keep the one-shot marker inside
# the tmux server so it disappears naturally when that server exits.
restore_state="$("$TMUX_BIN" show-option -gv "$RESTORE_STATE_OPTION" 2>/dev/null || true)"
case "$restore_state" in
  "$server_pid:complete")
    log "restore already complete for tmux server pid=$server_pid; skipping duplicate invocation"
    exit 0
    ;;
  "$server_pid:in-progress:"*)
    restore_owner="${restore_state##*:}"
    if printf '%s' "$restore_owner" | grep -Eq '^[0-9]+$' && kill -0 "$restore_owner" 2>/dev/null; then
      log "restore already in progress for tmux server pid=$server_pid owner=$restore_owner; skipping duplicate invocation"
      exit 0
    fi
    ;;
esac

"$TMUX_BIN" set-option -g "$RESTORE_STATE_OPTION" "$server_pid:in-progress:$$"
restore_state_claimed=1
trap finish_restore_state EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# tmux-resurrect assumes TMUX is present even when invoked outside an attached
# client. LaunchAgent processes do not inherit it, so construct the canonical
# socket,pid,pane value for the server we just started.
export TMUX="${socket_path},${server_pid},0"

before_panes="$(count_live_panes)"
log "restoring snapshot=$(readlink "$last_file" 2>/dev/null || printf '%s' "$last_file") expected_panes=$expected_panes live_before=$before_panes"

if ! "$RESTORE_SCRIPT"; then
  log "tmux-resurrect returned a failure"
  exit 1
fi

[ -f "$MOSHI_RECONCILER" ] && python3 "$MOSHI_RECONCILER" --quiet 2>/dev/null || true

after_panes="$(count_live_panes)"
if [ "$after_panes" -lt "$expected_panes" ]; then
  log "restore incomplete: expected_at_least=$expected_panes live_after=$after_panes"
  exit 1
fi

log "restore complete: expected_at_least=$expected_panes live_after=$after_panes"
exit 0
