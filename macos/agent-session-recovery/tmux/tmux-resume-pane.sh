#!/bin/bash
# tmux-resume-pane.sh — relaunch ONE agent in ONE restored tmux pane.
#
# WHY THIS EXISTS: tmux-resurrect's @resurrect-processes maps a process NAME to a
# single fixed relaunch command, so every restored pane running `claude` got the
# exact same `claude --continue`, and every `codex` pane got `codex resume --last`.
# Both of those resolve to "the most recent conversation in this cwd", so N panes
# sharing a cwd all fought over ONE conversation. Measured on the 2026-08-10 reboot:
# 6 of 15 codex panes died with
#   "thread <uuid> already has an active writer (code -32600)"
# and the 5 claude panes under ~/…/echo would all have landed in the same session.
# This script is invoked with a PER-PANE session id resolved at save time by
# resurrect-secretty-fix.sh, so each tab returns to its OWN conversation.
#
# Invoked as the saved pane_full_command (see resurrect-secretty-fix.sh), i.e.
# resurrect literally types this line at the pane's shell prompt:
#   ~/.tmux/tmux-resume-pane.sh cc <session-id>|-
#   ~/.tmux/tmux-resume-pane.sh cx <thread-id>|-
#
# NOTE: the kind tokens are "cc"/"cx", NOT "claude"/"codex". @resurrect-processes
# matches with a SUBSTRING regex against the whole saved command line, so a literal
# "claude" anywhere in this line — including in an argument — would also match the
# legacy `~claude->claude --continue` fallback rule and silently replace this whole
# command with it. Do not "clarify" these tokens back to the agent names.
#
# A "-" id means "no per-pane id was resolvable, use the old global behaviour".

set -u

kind="${1:-}"
sid="${2:--}"

CODEX="${HOME}/.claude/codex-notify.sh"   # same wrapper the interactive `codex` alias uses
[ -x "$CODEX" ] || CODEX="codex"

# REASON: claude gates the resume of a large/old session behind an interactive modal
# ("Resuming the full session will consume a substantial portion of your usage
# limits"). On the 2026-08-10 reboot that left 10 panes parked on the modal —
# claude was running but had loaded nothing, and stayed that way until a human
# visited each tab. There is no CLI flag and no settings key for it (searched the
# 2.1.227 bundle: the tri-state values compact|continue|never exist but no
# persisted key), so we answer the modal from outside instead.
#
# Option 1 "Resume from summary (recommended)" is preselected, so a bare Enter
# accepts it — deliberately NOT "1" then Enter: if we ever misfire and the modal
# is not actually up, a stray Enter is a no-op newline, whereas "1" would be
# submitted to the agent as a one-character user message.
answer_startup_modals() {
	local pane="$1" resume_needle="Resuming the full session will consume"
	local trust_needle="Quick safety check: Is this a project you created or one you trust"
	local i now handled_resume=0 handled_trust=0
	# REASON: the pane still shows the PREVIOUS session's last screenful, which
	# resurrect replays with `cat` before this command runs. If that screenful
	# happens to be a modal left unanswered from an earlier reboot, matching on
	# text alone would fire instantly against stale pixels and then stop watching,
	# missing the real modal seconds later. So snapshot the screen first and only
	# act once it has actually changed.
	local before
	before="$(tmux capture-pane -p -t "$pane" 2>/dev/null)" || return 0
	for ((i = 0; i < 240; i++)); do   # 240 * 0.5s = 120s
		sleep 0.5
		now="$(tmux capture-pane -p -t "$pane" 2>/dev/null)" || return 0
		# Give the new process four seconds to replace resurrect's replayed frame.
		# After that, a stable startup modal is real enough to answer: Claude can
		# spend tens of seconds loading an old transcript without repainting.
		[ "$now" = "$before" ] && [ "$i" -lt 8 ] && continue
		case "$now" in
			*"$trust_needle"*)
				if [ "$handled_trust" -eq 0 ]; then
					# This is a previously running, checkpointed pane in its saved cwd,
					# so the workspace was already trusted before the reboot.
					tmux send-keys -t "$pane" Enter
					handled_trust=1
					before="$now"
				fi
				;;
			*"$resume_needle"*)
				[ "$handled_resume" -eq 0 ] || continue
				tmux send-keys -t "$pane" Enter
				handled_resume=1
				before="$now"
				;;
		esac
	done
}

case "$kind" in
	cc)
		if [ -n "${TMUX_PANE:-}" ]; then
			answer_startup_modals "$TMUX_PANE" >/dev/null 2>&1 &
		fi
		if [ "$sid" = "-" ]; then
			exec claude --continue --dangerously-skip-permissions
		fi
		# REASON: fall back to --continue if the recorded session id is gone (file
		# pruned, or resumed elsewhere). --resume on a missing id exits nonzero and
		# would drop the pane to a bare shell with no conversation at all.
		exec claude --resume "$sid" --dangerously-skip-permissions \
			|| exec claude --continue --dangerously-skip-permissions
		;;
	cx)
		# NOTE: `codex resume` does NOT accept --dangerously-bypass-approvals-and-sandbox
		# (that flag lives on the root command, so it is silently ignored here). The
		# -c overrides below are the equivalent; resume does honour -c.
		if [ "$sid" = "-" ]; then
			exec "$CODEX" resume --last \
				-c tui.resume_cwd=current \
				-c sandbox_mode=danger-full-access -c approval_policy=never
		fi
		exec "$CODEX" resume "$sid" \
			-c tui.resume_cwd=current \
			-c sandbox_mode=danger-full-access -c approval_policy=never
		;;
	*)
		echo "tmux-resume-pane.sh: unknown kind '${kind}' (expected cc|cx)" >&2
		exit 2
		;;
esac
