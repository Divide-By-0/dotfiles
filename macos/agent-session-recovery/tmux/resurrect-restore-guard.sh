#!/bin/bash
# resurrect-restore-guard.sh — @resurrect-hook-pre-restore-all / -post-restore-all.
#
# WHY THIS EXISTS: ~/.zshrc has a precmd hook (_tmux_auto_rename_session) that
# renames a tmux session to the basename of the first pane's cwd. tmux-resurrect
# restores in two phases: it creates EVERY pane first, and only then walks the save
# file again to send each pane its relaunch command, addressing panes by the session
# name recorded in the save file. Restored shells reach their first prompt during
# phase one, so they rename sessions out from under phase two, and
#   _process_should_be_restored -> pane_exists "<old name>" -> false -> return 1
# skips the pane in SILENCE (not even the "not auto-restored" echo, which is the
# return-2 path). Measured on the 2026-08-10 reboot: 10 of 38 agent panes were
# dropped this way, every single one of them in a session that had been renamed
#   Documents -> .projects.nosync,  projects -> gmail-forwarder,
#   lxm_house -> lxm.house,         assembly-task-forge -> assembly-verifiers
# and reproduced 1:1 on an isolated socket (28/38 with the race, 38/38 without).
#
# pre-restore-all  : raise a flag the zshrc hooks check, freezing session renames.
# post-restore-all : lower it, then do the rename ONCE, centrally, now that every
#                    relaunch command has already been delivered.
#
# What breaks if this is removed: restores silently lose every agent pane whose
# session name does not already equal its cwd basename.

set -u

FLAG='@resurrect-restore-in-progress'
REAL_CWD="${HOME}/.tmux/real-cwd.sh"

case "${1:-}" in
	pre)
		# REASON: store an epoch, not a bare "1". If a restore dies between the two
		# hooks the flag would otherwise wedge session renaming off forever; the
		# zshrc side treats a stamp older than its own grace window as absent.
		tmux set-option -g "$FLAG" "$(date +%s)" 2>/dev/null
		;;
	post)
		tmux set-option -gu "$FLAG" 2>/dev/null

		# Reconcile session names the same way the zshrc precmd hook would have,
		# but after the fact and from one place. Panes that successfully relaunched
		# an agent never draw another shell prompt, so without this their sessions
		# would keep whatever name the save file had.
		tmux list-sessions -F '#{session_name}' 2>/dev/null | while IFS= read -r s; do
			# A trailing colon keeps tmux 3.7 from parsing dots in a session name as
			# pane separators (for example .projects.nosync and lxm.house).
			pane_pid="$(tmux list-panes -t "${s}:" -F '#{pane_pid}' 2>/dev/null | head -1)"
			[ -n "$pane_pid" ] || continue
			# REASON: #{pane_current_path} reports the secretty wrapper's frozen cwd,
			# not the shell's. real-cwd.sh walks the live process tree to see through it.
			dir="$([ -x "$REAL_CWD" ] && "$REAL_CWD" "$pane_pid" 2>/dev/null)"
			[ -n "$dir" ] && [ "$dir" != "/" ] || continue
			# Silent on failure: tmux rejects a rename to an already-taken name, which
			# is normal here (several sessions legitimately sit in the same directory).
			tmux rename-session -t "${s}:" "${dir##*/}" 2>/dev/null || true
		done
		;;
	*)
		echo "usage: $0 pre|post" >&2
		exit 2
		;;
esac

exit 0
