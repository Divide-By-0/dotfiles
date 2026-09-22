#!/bin/sh
# Unified Moshi Claude Code hook entrypoint (cmux-aware).
#
# Moshi binds terminals by inspecting the process that runs `claude-hook`.
# Cmux Claudes are Ghostty surfaces (no $TMUX_PANE) and parent-walk often
# steals an unrelated secretty/tmux pane. Fix: ensure a per-surface ghost
# tmux session exists, and run *every* moshi hook inside that pane via a
# queue drained by the ghost keeper.
set -u
LOG="${HOME}/.claude/hooks/moshi-sessionstart.log"
MOSHI="${MOSHI_BIN:-/opt/homebrew/bin/moshi}"
KEEPER="${HOME}/.claude/hooks/moshi-ghost-keeper.sh"
TITLE_PY="${HOME}/.claude/hooks/moshi-cmux-title.py"
RECONCILER="${HOME}/.tmux/reconcile-moshi-sessions.py"
STATE_DIR="${HOME}/.claude/hooks/moshi-cmux-binds"
mkdir -p "$STATE_DIR" 2>/dev/null || true

log() {
  echo "---- $(date '+%Y-%m-%dT%H:%M:%S%z') ----" >>"$LOG" 2>/dev/null || true
  echo "$*" >>"$LOG" 2>/dev/null || true
}

input=$(cat || true)
[ -n "$input" ] || exit 0

event=$(printf '%s' "$input" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("hook_event_name") or "")' 2>/dev/null || true)
session_id=$(printf '%s' "$input" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("session_id") or "")' 2>/dev/null || true)
cwd=$(printf '%s' "$input" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("cwd") or "")' 2>/dev/null || true)
[ -n "$cwd" ] || cwd="$PWD"

surface_id="${CMUX_SURFACE_ID:-}"
workspace_id="${CMUX_WORKSPACE_ID:-}"

forward_local() {
  case "$event" in
    PermissionRequest|PreToolUse)
      printf '%s\n' "$input" | env MOSHI_DISABLE_PARENT_TERMINAL_LOOKUP=1 "$MOSHI" claude-hook "$@"
      return $?
      ;;
  esac
  printf '%s\n' "$input" | env MOSHI_DISABLE_PARENT_TERMINAL_LOOKUP=1 \
    "$MOSHI" claude-hook "$@" >/dev/null 2>>"$LOG" || true
  return 0
}

# Real multiplexer already? Run locally (still disable parent walk).
if [ -n "${TMUX_PANE:-}" ] || [ -n "${ZELLIJ:-}" ] || [ "${HERDR_ENV:-}" = "1" ]; then
  forward_local "$@"
  exit $?
fi

# Not cmux: best-effort local forward.
if [ -z "$surface_id" ]; then
  forward_local "$@"
  exit $?
fi

# cmux: all sibling horizontal tabs share one tmux session. Fail closed:
# falling back to local parent lookup could bind an unrelated terminal.
GROUPS="${HOME}/.claude/hooks/moshi-cmux-groups.py"
printf '%s\n' "$input" | python3 "$GROUPS" --hook
exit $?
