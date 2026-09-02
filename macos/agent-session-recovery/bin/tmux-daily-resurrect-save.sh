#!/bin/bash
set -euo pipefail

: "${HOME:?HOME must be set by launchd or the calling shell}"
export PATH="${SESSION_RESTORE_PATH:-/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
SAVE_SCRIPT="$HOME/.tmux/plugins/tmux-resurrect/scripts/save.sh"
RESURRECT_DIR="$HOME/.tmux/resurrect"
ARCHIVE_DIR="$RESURRECT_DIR/daily"
KEEP="${TMUX_DAILY_RESURRECT_KEEP:-14}"
LOCK_DIR="$RESURRECT_DIR/.resurrect-save.lock"
archive=1

case "${1:-}" in
  "") ;;
  --checkpoint-only) archive=0 ;;
  *)
    echo "usage: $0 [--checkpoint-only]" >&2
    exit 2
    ;;
esac

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

release_lock() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

acquire_lock() {
  mkdir -p "$RESURRECT_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_DIR/pid"
    trap release_lock EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    return 0
  fi

  lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  lock_command=""
  if printf '%s' "$lock_pid" | grep -Eq '^[0-9]+$' && kill -0 "$lock_pid" 2>/dev/null; then
    lock_command="$(ps -p "$lock_pid" -o command= 2>/dev/null || true)"
  fi
  case "$lock_command" in
    *tmux-daily-resurrect-save.sh*|*tmux-periodic-resurrect-save.sh*)
      log "another tmux-resurrect save is already running pid=$lock_pid; skipping"
      return 1
      ;;
  esac

  rm -f "$LOCK_DIR/pid"
  if ! rmdir "$LOCK_DIR" 2>/dev/null || ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "could not recover stale save lock: $LOCK_DIR"
    return 1
  fi
  printf '%s\n' "$$" >"$LOCK_DIR/pid"
  trap release_lock EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

if [ ! -x "$TMUX_BIN" ]; then
  log "tmux binary missing: $TMUX_BIN"
  exit 0
fi

if [ ! -x "$SAVE_SCRIPT" ]; then
  log "tmux-resurrect save script missing: $SAVE_SCRIPT"
  exit 0
fi

if ! "$TMUX_BIN" has-session >/dev/null 2>&1; then
  log "no default tmux server running; nothing to save"
  exit 0
fi

acquire_lock || exit 0
mkdir -p "$ARCHIVE_DIR"

if [ -z "${TMUX:-}" ]; then
  socket_path="$("$TMUX_BIN" display-message -p '#{socket_path}' 2>/dev/null || true)"
  server_pid="$("$TMUX_BIN" display-message -p '#{pid}' 2>/dev/null || true)"
  if [ -n "$socket_path" ] && printf '%s' "$server_pid" | grep -Eq '^[0-9]+$'; then
    # REASON: tmux-resurrect's save script emits an empty layout when launched
    # by launchd without a client context. Supplying the default server's TMUX
    # value makes the non-interactive save match a real attached-client save.
    export TMUX="${socket_path},${server_pid},0"
  fi
fi

last_link="$RESURRECT_DIR/last"
previous_target="$(readlink "$last_link" 2>/dev/null || true)"

log "saving tmux-resurrect state"
"$SAVE_SCRIPT" quiet

if [ ! -e "$last_link" ]; then
  log "save finished but $last_link does not exist"
  exit 1
fi

last_target="$(readlink "$last_link" 2>/dev/null || true)"
if [ -n "$last_target" ]; then
  case "$last_target" in
    /*) last_file="$last_target" ;;
    *) last_file="$RESURRECT_DIR/$last_target" ;;
  esac
else
  last_file="$last_link"
fi

if [ ! -f "$last_file" ]; then
  log "last resurrect file missing: $last_file"
  exit 1
fi

pane_count="$(awk -F '\t' '$1 == "pane" { count++ } END { print count + 0 }' "$last_file")"
window_count="$(awk -F '\t' '$1 == "window" { count++ } END { print count + 0 }' "$last_file")"
if [ "$pane_count" -eq 0 ] || [ "$window_count" -eq 0 ]; then
  log "invalid resurrect save: panes=$pane_count windows=$window_count file=$last_file"
  if [ -n "$previous_target" ]; then
    ln -sfn "$previous_target" "$last_link"
    log "restored previous last symlink: $previous_target"
  fi
  exit 1
fi

"$TMUX_BIN" set-option -g @agent-session-save-last-timestamp "$(date +%s)" 2>/dev/null || true

if [ "$archive" -eq 0 ]; then
  log "checkpoint complete: panes=$pane_count windows=$window_count file=$last_file"
  exit 0
fi

stamp="$(date '+%Y%m%dT%H%M%S')"
snapshot="$ARCHIVE_DIR/tmux_resurrect_daily_$stamp.txt"
contents="$ARCHIVE_DIR/pane_contents_daily_$stamp.tar.gz"

cp -p "$last_file" "$snapshot"
if [ -f "$RESURRECT_DIR/pane_contents.tar.gz" ]; then
  cp -p "$RESURRECT_DIR/pane_contents.tar.gz" "$contents"
fi
ln -sfn "$(basename "$snapshot")" "$ARCHIVE_DIR/latest"

log "archived daily snapshot: $snapshot"

count=0
find "$ARCHIVE_DIR" -maxdepth 1 -type f -name 'tmux_resurrect_daily_*.txt' | sort -r |
while IFS= read -r file; do
  count=$((count + 1))
  if [ "$count" -le "$KEEP" ]; then
    continue
  fi

  base="$(basename "$file" .txt)"
  suffix="${base#tmux_resurrect_daily_}"
  rm -f "$file" "$ARCHIVE_DIR/pane_contents_daily_$suffix.tar.gz"
  log "pruned old daily snapshot: $file"
done

find "$ARCHIVE_DIR" -maxdepth 1 -type f -name 'pane_contents_daily_*.tar.gz' |
while IFS= read -r file; do
  base="$(basename "$file" .tar.gz)"
  suffix="${base#pane_contents_daily_}"
  if [ ! -f "$ARCHIVE_DIR/tmux_resurrect_daily_$suffix.txt" ]; then
    rm -f "$file"
    log "pruned orphan pane contents archive: $file"
  fi
done

exit 0
