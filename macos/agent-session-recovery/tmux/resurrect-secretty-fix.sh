#!/bin/bash
# resurrect-secretty-fix.sh — tmux-resurrect @resurrect-hook-post-save-all hook.
#
# REASON: every pane here is wrapped by `secretty` (secretty -> zsh -> [bash ->]
# codex/claude/agent). tmux's #{pane_current_path} and #{pane_full_command}
# (which resurrect saves) report the OUTER secretty process: its cwd is frozen at
# launch (usually /) and its command is always the secretty-launched login shell
# (/bin/zsh -l -i). So resurrect saved cwd=/ and full_command=/bin/zsh for every
# pane, which meant restores landed in / and never resumed codex/claude/agent.
# tmux can't see through secretty, but `pgrep`/`lsof` on the live tree can.
#
# This hook rewrites the just-saved file (the one ~/.tmux/resurrect/last points to):
#   * field 8  (dir / pane_current_path) -> the interactive zsh child's REAL cwd,
#     i.e. where the user actually is / where the agent was launched. Done for
#     every pane, so restores land in the right directory.
#   * field 11 (pane_full_command) -> the exact relaunch line for that ONE pane.
#     field 10 (pane_current_command) is set to the agent name, for readability.
#
# PER-PANE SESSION IDS (added 2026-08-11). This used to write a bare "codex" /
# "claude" into field 11 and let @resurrect-processes map that name to one fixed
# command. Both of those fixed commands mean "the most recent conversation in this
# cwd" (`claude --continue`, `codex resume --last`), so N panes sharing a cwd all
# resolved to the SAME conversation. On the 2026-08-10 reboot that killed 6 of 15
# codex panes outright — the first pane took the thread's writer lock and the rest
# died with "thread <uuid> already has an active writer (code -32600)" — and the
# five claude panes under ~/…/echo would all have re-entered one session. So we now
# resolve a specific id per pane and emit
#     ~/.tmux/tmux-resume-pane.sh cc|cx <id>
# falling back to "-" (= the old global behaviour) when no id can be resolved.
#
# Where the ids come from, and why they differ per agent:
#   codex  — EXACT. A live codex holds an exclusive fd on
#            ~/.codex/thread-writer-locks/<thread>.lock, so lsof on the pane's codex
#            pid names its thread. Verified one lock per pane process.
#   claude — EXACT when ~/.claude/hooks/tmux-pane-session.sh recorded it at
#            SessionStart, which is the normal case going forward. A running claude
#            holds NO fd on its ~/.claude/projects/<enc-cwd>/<id>.jsonl (it opens,
#            appends, closes — checked on 2.1.227, lsof shows only ttys/sockets), so
#            for sessions that predate the hook we fall back to ranking that
#            directory's transcripts by mtime and handing each pane a DISTINCT one,
#            newest first. Rank 0 is exactly what --continue would have picked, so
#            the common one-pane-per-directory case is unchanged; where several
#            panes share a directory the pairing may permute, which is still
#            strictly better than all of them landing on one conversation.
#
# restore.sh field order (scripts/restore.sh:178, tab-delimited, 11 fields):
#   1 line_type 2 session 3 window 4 win_active 5 win_flags 6 pane_index
#   7 pane_title 8 :dir 9 pane_active 10 pane_command 11 :pane_full_command
# The pid is NOT in this (deployed) format, so we look it up live via session:win.pane
# (fields 2/3/6), which are stable across resurrect versions.
#
# FAIL-SAFE: any inconsistency (line-count mismatch, empty output, missing file)
# leaves the original save untouched — a corrupted save means a failed restore at
# next reboot, so correctness > completeness.
#
# NOTE: uses awk (FS=OFS='\t') to rewrite fields, NOT bash `read -a` with IFS=tab:
# bash treats tab as IFS-whitespace and collapses consecutive tabs, shifting field
# positions whenever a field (e.g. window_flags) is empty. Do not "simplify" this.
#
# NOTE: /bin/bash on macOS is 3.2 — no associative arrays. The "already claimed"
# bookkeeping below is a plain space-delimited string on purpose.

set -u
RDIR="${HOME}/.tmux/resurrect"
PANE_SESSIONS="${HOME}/.tmux/pane-sessions"
CLAUDE_LIVE="${HOME}/.tmux/claude-live"
RESUME_SH="${HOME}/.tmux/tmux-resume-pane.sh"
last_name="$(readlink "${RDIR}/last" 2>/dev/null)" || exit 0
[ -n "$last_name" ] || exit 0
SAVE="${RDIR}/${last_name}"
[ -f "$SAVE" ] || exit 0
command -v tmux >/dev/null 2>&1 || exit 0

SERVER_PID="$(tmux display-message -p '#{pid}' 2>/dev/null)"

cwd_of() {  # real cwd of a pid (macOS: via lsof)
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | awk '/^n/{sub(/^n/,"");print;exit}'
}

is_plugin_cwd() {
  case "$1" in
    "$HOME/.claude/plugins"|"$HOME/.claude/plugins/"*|\
    "$HOME/.codex/plugins"|"$HOME/.codex/plugins/"*) return 0 ;;
    *) return 1 ;;
  esac
}

real_cwd() {  # deepest non-"/", non-plugin cwd among $1 and descendants, else ""
  # REASON: agents cd into the project dir in a DEEP child (e.g. codex's node_repl),
  # while the secretty/zsh/codex processes above it stay at /. The deepest non-root
  # cwd is the actual working directory. For a plain shell the zsh leaf holds it.
  # Ignore Claude/Codex plugin package directories: plugin servers are long-lived
  # descendants, but their installation path is not the pane's working directory.
  local best="" bestdepth=-1 queue="$1:0" nextq item p d c k
  while [ -n "$queue" ]; do
    nextq=""
    for item in $queue; do
      p="${item%%:*}"; d="${item##*:}"
      c="$(cwd_of "$p")"
      if [ -n "$c" ] && [ "$c" != "/" ] && ! is_plugin_cwd "$c" && [ "$d" -gt "$bestdepth" ]; then
        best="$c"; bestdepth="$d"
      fi
      for k in $(pgrep -P "$p" 2>/dev/null); do nextq="$nextq ${k}:$((d+1))"; done
    done
    queue="$nextq"
  done
  printf '%s' "$best"
}

agent_in_tree() {  # echo "<codex|claude|agent> <pid>" for the first hit, else ""
  local queue="$1" next p comm base
  while [ -n "$queue" ]; do
    next=""
    for p in $queue; do
      comm="$(ps -o comm= -p "$p" 2>/dev/null)"
      base="${comm##*/}"
      case "$comm" in
        *codex*)  echo "codex $p";  return ;;
        *claude*) echo "claude $p"; return ;;
      esac
      [ "$base" = "agent" ] && { echo "agent $p"; return; }
      next="$next $(pgrep -P "$p" 2>/dev/null)"
    done
    queue="$next"
  done
  echo ""
}

codex_thread_of() {  # exact thread uuid for a codex pid, else ""
  # REASON: codex takes an exclusive fd on its thread's writer lock for the whole
  # session — the same lock whose contention produced the "already has an active
  # writer" failures. That makes it an exact pid -> thread map. If a pid somehow
  # holds several (a shared app-server process would), refuse to guess and let the
  # caller fall back to --last.
  local locks n
  locks="$(lsof -p "$1" 2>/dev/null |
           grep -oE 'thread-writer-locks/[0-9a-fA-F-]+\.lock' |
           sed 's|.*/||; s|\.lock$||' | sort -u)"
  n="$(printf '%s\n' "$locks" | grep -c '[0-9a-fA-F]')"
  [ "$n" = "1" ] || return 0
  printf '%s' "$locks"
}

claude_project_dir() {  # ~/.claude/projects/<encoded cwd> for a directory
  # Claude encodes the cwd by replacing both "/" and "." with "-", e.g.
  #   /Users/x/Documents/.projects.nosync/echo
  #   -> -Users-x-Documents--projects-nosync-echo
  printf '%s/.claude/projects/%s' "$HOME" \
    "$(printf '%s' "$1" | sed 's|/|-|g; s|\.|-|g')"
}

CLAIMED_CLAUDE=" "   # space-delimited set of session ids already handed to a pane

claude_session_of() {  # exact id from the SessionStart hook, else ""
  local pane_id="$1" f id
  [ -n "$pane_id" ] && [ -n "$SERVER_PID" ] || return 0
  f="${PANE_SESSIONS}/${SERVER_PID}-${pane_id#%}.pane"
  [ -f "$f" ] || return 0
  id="$(head -1 "$f" 2>/dev/null)"
  [ -n "$id" ] || return 0
  # Only trust it if the transcript is actually still on disk.
  [ -f "$(claude_project_dir "$2")/${id}.jsonl" ] || return 0
  printf '%s' "$id"
}

claude_session_fallback() {  # newest UNCLAIMED transcript in this cwd, else ""
  # $1 = pane cwd, $2 = this pane's claude pid
  local pdir f id owner
  pdir="$(claude_project_dir "$1")"
  [ -d "$pdir" ] || return 0
  for f in $(ls -t "$pdir"/*.jsonl 2>/dev/null); do
    id="${f##*/}"; id="${id%.jsonl}"
    case "$CLAIMED_CLAUDE" in *" $id "*) continue ;; esac
    # REASON: skip transcripts a DIFFERENT live claude is sitting in. Sessions
    # running outside tmux (cmux/Ghostty surfaces) own no pane, so nothing else
    # claims them, yet they hold the newest transcript in their project directory —
    # handing one to a pane would put two claudes in one conversation. The registry
    # is written by ~/.claude/hooks/tmux-pane-session.sh at SessionStart.
    owner="$(head -1 "$CLAUDE_LIVE/$id" 2>/dev/null)"
    if [ -n "$owner" ] && [ "$owner" != "$2" ] && kill -0 "$owner" 2>/dev/null; then
      continue
    fi
    printf '%s' "$id"
    return 0
  done
}

# REASON: pre-claim every id the SessionStart hook already owns, BEFORE the
# single pass below. Otherwise an earlier mtime-fallback pane could hand itself an
# id that a later pane holds an exact record for, putting two panes in one session
# — precisely the bug this whole change exists to remove.
if [ -d "$PANE_SESSIONS" ] && [ -n "$SERVER_PID" ]; then
  for f in "$PANE_SESSIONS/${SERVER_PID}-"*.pane; do
    [ -e "$f" ] || continue
    id="$(head -1 "$f" 2>/dev/null)"
    [ -n "$id" ] && CLAIMED_CLAUDE="${CLAIMED_CLAUDE}${id} "
  done
fi

TMP="$(mktemp)" || exit 0
trap 'rm -f "$TMP"' EXIT

while IFS= read -r line || [ -n "$line" ]; do
  if [ "${line%%	*}" = "pane" ]; then
    session="$(printf '%s' "$line" | cut -f2)"
    win="$(printf '%s' "$line" | cut -f3)"
    pane="$(printf '%s' "$line" | cut -f6)"
    target="${session}:${win}.${pane}"
    pid="$(tmux list-panes -t "$target" -F '#{pane_pid}' 2>/dev/null | head -1)"
    pane_id="$(tmux list-panes -t "$target" -F '#{pane_id}' 2>/dev/null | head -1)"
    if printf '%s' "$pid" | grep -Eq '^[0-9]+$' && kill -0 "$pid" 2>/dev/null; then
      rcwd="$(real_cwd "$pid")"
      hit="$(agent_in_tree "$pid")"
      ag="${hit%% *}"
      agpid="${hit##* }"
      cmd=""
      case "$ag" in
        codex)
          sid="$(codex_thread_of "$agpid")"
          cmd="$RESUME_SH cx ${sid:--}"
          ;;
        claude)
          sid="$(claude_session_of "$pane_id" "${rcwd:-$HOME}")"
          [ -n "$sid" ] || sid="$(claude_session_fallback "${rcwd:-$HOME}" "$agpid")"
          [ -n "$sid" ] && CLAIMED_CLAUDE="${CLAIMED_CLAUDE}${sid} "
          cmd="$RESUME_SH cc ${sid:--}"
          ;;
        agent)
          # cursor-agent has no per-session handle to key on; keep the old
          # name-matched rule (~agent->agent --continue -f) doing the work.
          cmd="agent"
          ;;
      esac
      if [ -n "$rcwd" ] || [ -n "$cmd" ]; then
        line="$(printf '%s' "$line" | awk -F'\t' -v OFS='\t' \
                 -v c="$rcwd" -v a="$ag" -v m="$cmd" \
                 '{ if (c != "") $8=":" c; if (a != "") $10=a; if (m != "") $11=":" m; print }')"
      fi
    fi
  fi
  printf '%s\n' "$line" >> "$TMP"
done < "$SAVE"

# Fail-safe: only overwrite if every line was preserved.
if [ -s "$TMP" ] && [ "$(wc -l < "$TMP")" -eq "$(wc -l < "$SAVE")" ]; then
  cat "$TMP" > "$SAVE"
fi
