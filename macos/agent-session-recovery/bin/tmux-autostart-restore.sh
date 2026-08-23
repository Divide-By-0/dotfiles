#!/bin/bash
set -u

: "${HOME:?HOME must be set by launchd or the calling shell}"
export PATH="${SESSION_RESTORE_PATH:-/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
RESTORE_SCRIPT="$HOME/.tmux/plugins/tmux-resurrect/scripts/restore.sh"
RESURRECT_DIR="$HOME/.tmux/resurrect"
BOOTSTRAP_SESSION="autostart"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

count_snapshot_panes() {
  awk -F '\t' '$1 == "pane" { count++ } END { print count + 0 }' "$1"
}

count_live_panes() {
  "$TMUX_BIN" list-panes -a -F '#{pane_id}' 2>/dev/null | wc -l | tr -d ' '
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

after_panes="$(count_live_panes)"
if [ "$after_panes" -lt "$expected_panes" ]; then
  log "restore incomplete: expected_at_least=$expected_panes live_after=$after_panes"
  exit 1
fi

log "restore complete: expected_at_least=$expected_panes live_after=$after_panes"
exit 0
