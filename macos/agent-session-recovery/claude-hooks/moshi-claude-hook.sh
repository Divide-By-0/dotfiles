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

# --- cmux: ensure ghost session + enqueue hook into it ---
if ! command -v tmux >/dev/null 2>&1; then
  log "cmux hook but tmux missing event=$event session=$session_id"
  forward_local "$@"
  exit 0
fi

surf_key=$(printf '%s' "$surface_id" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-f0-9' | cut -c1-12)
[ -n "$surf_key" ] || surf_key=$(printf '%s' "$session_id" | tr -cd 'a-f0-9' | cut -c1-12)
[ -n "$surf_key" ] || surf_key="unknown"
queue_dir="${STATE_DIR}/${surf_key}.queue"
state_file="${STATE_DIR}/${surf_key}.json"
start_script="${STATE_DIR}/${surf_key}.start.sh"
mkdir -p "$queue_dir" 2>/dev/null || true

title="cmux"
desired_session="tab-${surf_key}"
if [ -f "$TITLE_PY" ]; then
  title=$(python3 "$TITLE_PY" --title "$surface_id" 2>/dev/null || echo cmux)
  desired_session=$(python3 "$TITLE_PY" --session-name "$surface_id" "$surf_key" 2>/dev/null || echo "tab-${surf_key}")
fi
[ -n "$title" ] || title="cmux"
[ -n "$desired_session" ] || desired_session="tab-${surf_key}"

# Resolve which tmux session already backs this surface (legacy hex name or prior title).
legacy_session="moshi-cmux-${surf_key}"
tmux_session="$desired_session"
if [ -f "$state_file" ]; then
  prev=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("tmux_session") or "")' "$state_file" 2>/dev/null || true)
  if [ -n "$prev" ] && tmux has-session -t "$prev" 2>/dev/null; then
    tmux_session="$prev"
  fi
fi
if ! tmux has-session -t "$tmux_session" 2>/dev/null; then
  if tmux has-session -t "$legacy_session" 2>/dev/null; then
    tmux_session="$legacy_session"
  fi
fi

# Rename legacy/outdated session names to the human tab title Moshi shows.
if [ "$tmux_session" != "$desired_session" ] && tmux has-session -t "$tmux_session" 2>/dev/null; then
  if tmux has-session -t "$desired_session" 2>/dev/null; then
    # Collision: keep unique desired name with an extra suffix.
    desired_session="${desired_session}-x"
  fi
  if tmux rename-session -t "$tmux_session" "$desired_session" 2>>"$LOG"; then
    log "renamed tmux session $tmux_session -> $desired_session title=$title"
    tmux_session="$desired_session"
  fi
fi

# Persist surface metadata for the long-lived keeper (no respawn needed).
python3 - "$state_file" "$session_id" "$cwd" "$surface_id" "$workspace_id" "$title" "$tmux_session" <<'PY'
import json, sys
from pathlib import Path
path, session_id, cwd, surface_id, workspace_id, title, tmux_session = sys.argv[1:8]
Path(path).write_text(json.dumps({
    "session_id": session_id,
    "cwd": cwd,
    "surface_id": surface_id,
    "workspace_id": workspace_id,
    "title": title,
    "tmux_session": tmux_session,
}, indent=2) + "\n")
PY

# Window name is what tmux status / some clients show; keep it human.
win_name=$(printf '%s' "$title" | tr -cd '[:alnum:][:space:]\-_' | cut -c1-40)
[ -n "$win_name" ] || win_name="$desired_session"
cat >"$start_script" <<START
#!/bin/sh
set -u
exec "$KEEPER" "$state_file" "$queue_dir"
START
chmod +x "$start_script"

ghost_alive() {
  tmux has-session -t "$tmux_session" 2>/dev/null || return 1
  cmd=$(tmux display-message -p -t "${tmux_session}:0" '#{pane_current_command}' 2>/dev/null || true)
  case "$cmd" in
    bash|sh|zsh|python|python3|python3.*) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_ghost() {
  if ghost_alive; then
    tmux rename-window -t "${tmux_session}:0" "$win_name" 2>/dev/null || true
    tmux select-pane -t "${tmux_session}:0" -T "$title" 2>/dev/null || true
    return 0
  fi

  if tmux has-session -t "$tmux_session" 2>/dev/null; then
    tmux set-option -t "$tmux_session" remain-on-exit on 2>/dev/null || true
    tmux rename-window -t "${tmux_session}:0" "$win_name" 2>/dev/null || true
    tmux respawn-pane -k -t "${tmux_session}:0" "$start_script" 2>>"$LOG" || {
      tmux kill-session -t "$tmux_session" 2>/dev/null || true
      tmux new-session -d -s "$tmux_session" -n "$win_name" -c "$cwd" "$start_script" 2>>"$LOG" || return 1
    }
  else
    tmux new-session -d -s "$tmux_session" -n "$win_name" -c "$cwd" "$start_script" 2>>"$LOG" || {
      log "failed creating ghost tmux=$tmux_session"
      return 1
    }
  fi
  tmux set-option -t "$tmux_session" remain-on-exit on 2>/dev/null || true
  tmux select-pane -t "${tmux_session}:0" -T "$title" 2>/dev/null || true
  # Fresh keeper needs a beat before the drain loop is ready.
  sleep 0.4
  return 0
}

ensure_ghost || {
  forward_local "$@"
  exit 0
}

# Enqueue work for the in-pane keeper.
req_id="${event:-evt}-$(date +%s)-$$-$RANDOM"
req="${queue_dir}/req-${req_id}.json"
donef="${queue_dir}/req-${req_id}.done"
printf '%s\n' "$input" >"$req"

# Permission/tool hooks need the result. Lifecycle hooks are async in Claude
# and often get killed on process exit — wait only briefly, then let the
# keeper finish in the background.
needs_result=0
timeout_s=8
case "$event" in
  PermissionRequest)
    needs_result=1
    timeout_s=130
    ;;
  PreToolUse)
    needs_result=1
    timeout_s=20
    ;;
  SessionStart)
    # Ensure the first bind lands before Claude continues.
    timeout_s=15
    ;;
  *)
    timeout_s=3
    ;;
esac

i=0
while [ "$i" -lt $((timeout_s * 4)) ]; do
  if [ -f "$donef" ]; then
    ec=$(cat "$donef" 2>/dev/null || echo 0)
    outf="${queue_dir}/req-${req_id}.out"
    if [ "$needs_result" -eq 1 ]; then
      [ -f "$outf" ] && cat "$outf"
      rm -f "$donef" "$outf" "${queue_dir}/req-${req_id}.err" 2>/dev/null || true
      exit "${ec:-0}"
    fi
    rm -f "$donef" "$outf" 2>/dev/null || true
    log "cmux queued hook ok event=$event session=$session_id tmux=$tmux_session ec=${ec:-0}"
    exit 0
  fi
  i=$((i + 1))
  sleep 0.25
done

if [ "$needs_result" -eq 1 ]; then
  log "cmux queued hook timeout event=$event session=$session_id tmux=$tmux_session"
  exit 1
fi

# Lifecycle: request is still queued/processing; keeper will finish it.
log "cmux queued hook detached event=$event session=$session_id tmux=$tmux_session"
exit 0
