#!/bin/bash
# tmux-pane-session.sh — SessionStart hook. Records "this tmux pane is running
# THIS claude session id" so tmux-resurrect can restore each tab into its own
# conversation instead of every tab racing for the newest one.
#
# WHY THIS EXISTS: unlike codex (which holds an exclusive fd on
# ~/.codex/thread-writer-locks/<thread>.lock, so the owning pid is discoverable
# with lsof), a running claude holds NO handle on its ~/.claude/projects/<enc-cwd>/
# <session-id>.jsonl — it opens, appends and closes. Verified on 2.1.227: lsof on a
# live claude pid shows only ttys and sockets. So there is no way to ask the OS
# which session a given pane's claude belongs to; it has to be recorded at the one
# moment both facts are known together, which is here.
#
# Read back by ~/.tmux/resurrect-secretty-fix.sh at save time.
#
# Keyed by tmux SERVER PID + pane id, not pane id alone: pane ids (%42) restart
# from low numbers on every new tmux server, so a plain %42 file from a previous
# boot would be silently mismatched onto an unrelated pane after a reboot.
#
# Always exits 0 — a hook failure must never surface in the Claude UI.

set -u

OUT_DIR="${HOME}/.tmux/pane-sessions"
LIVE_DIR="${HOME}/.tmux/claude-live"

input="$(cat 2>/dev/null || true)"
[ -n "$input" ] || exit 0

session_id="$(printf '%s' "$input" |
	python3 -c 'import sys,json; print(json.load(sys.stdin).get("session_id") or "")' 2>/dev/null || true)"
[ -n "$session_id" ] || exit 0

# REASON: record EVERY live claude, tmux or not, keyed by session id and holding
# the owning pid. resurrect-secretty-fix.sh's mtime fallback picks the newest
# unclaimed transcript in a directory, and "unclaimed" has to mean "no other claude
# is in it right now" — not just "no tmux pane holds it". Claude Code sessions
# started outside tmux (a cmux/Ghostty surface, where moshi-sessionstart.sh builds a
# ghost tmux session that owns no real pane) still write the newest transcript in
# their project directory, and without this registry the fallback happily hands that
# live conversation to an unrelated pane, putting two claudes in one transcript.
# Caught exactly that way on 2026-08-11: this very session, running outside tmux in
# ~/…/normal, was about to be handed to the lowprinormal:2.0 pane.
claude_pid=""
p="$PPID"
for _ in 1 2 3 4 5 6; do
	[ -n "$p" ] && [ "$p" != "1" ] || break
	case "$(ps -o comm= -p "$p" 2>/dev/null)" in
		*claude*) claude_pid="$p"; break ;;
	esac
	p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
done
[ -n "$claude_pid" ] || claude_pid="$PPID"

if mkdir -p "$LIVE_DIR" 2>/dev/null; then
	# Reap records whose owning process is gone (claude was SIGKILLed, machine
	# rebooted, …) so a dead session never blocks the fallback forever.
	for f in "$LIVE_DIR"/*; do
		[ -e "$f" ] || continue
		old="$(head -1 "$f" 2>/dev/null)"
		printf '%s' "$old" | grep -Eq '^[0-9]+$' || { rm -f "$f" 2>/dev/null; continue; }
		kill -0 "$old" 2>/dev/null || rm -f "$f" 2>/dev/null
	done
	printf '%s\n' "$claude_pid" > "${LIVE_DIR}/${session_id}" 2>/dev/null
fi

# The rest is the pane<->session record, which only means anything inside tmux.
[ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || exit 0

# $TMUX is "<socket-path>,<server-pid>,<session-index>"
server_pid="$(printf '%s' "$TMUX" | cut -d, -f2)"
printf '%s' "$server_pid" | grep -Eq '^[0-9]+$' || exit 0

mkdir -p "$OUT_DIR" 2>/dev/null || exit 0

# REASON: drop records from tmux servers that no longer exist. Without this the
# directory grows one stale file per pane per reboot forever, and a recycled
# server pid could match an old record.
for f in "$OUT_DIR"/*.pane; do
	[ -e "$f" ] || continue
	old_pid="${f##*/}"; old_pid="${old_pid%%-*}"
	printf '%s' "$old_pid" | grep -Eq '^[0-9]+$' || continue
	[ "$old_pid" = "$server_pid" ] && continue
	kill -0 "$old_pid" 2>/dev/null || rm -f "$f" 2>/dev/null
done

printf '%s\n' "$session_id" > "${OUT_DIR}/${server_pid}-${TMUX_PANE#%}.pane" 2>/dev/null

exit 0
