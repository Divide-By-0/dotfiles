#!/bin/sh
# Unified Moshi Claude Code hook entrypoint (cmux-aware).
#
# Moshi binds terminals by inspecting the process that runs `claude-hook`.
# Cmux Claudes are Ghostty surfaces (no $TMUX_PANE) and parent-walk often
# steals an unrelated secretty/tmux pane. Group sibling cmux tabs in tmux
# and forward hooks through the queue drained by each tab's mirror.
set -u
LOG="${HOME}/.claude/hooks/moshi-sessionstart.log"
MOSHI="${MOSHI_BIN:-/opt/homebrew/bin/moshi}"

input=$(cat || true)
[ -n "$input" ] || exit 0

# cmux hooks do not need to parse the payload or reconcile legacy ghost state.
# Fail closed: local parent lookup could bind an unrelated terminal.
if [ -n "${CMUX_SURFACE_ID:-}" ] && [ -z "${TMUX_PANE:-}" ] &&
   [ -z "${ZELLIJ:-}" ] && [ "${HERDR_ENV:-}" != "1" ]; then
  printf '%s\n' "$input" | "$HOME/.local/bin/moshi-cmux" --hook
  exit $?
fi

event=$(printf '%s' "$input" | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin).get("hook_event_name") or "")' 2>/dev/null || true)

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

forward_local "$@"
exit $?
