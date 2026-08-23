# Emit OSC 7 (working-directory report) on cd and at each prompt.
# REASON: the patched secretty (PR'd to its repo) follows OSC 7 to keep its own cwd
# in sync with this shell, so tmux's #{pane_current_path} becomes correct through the
# wrapper. zsh doesn't emit OSC 7 by default. Spaces are %-encoded; other exotic
# chars in paths are rare here. Harmless in plain terminals (many consume OSC 7 too).
_emit_osc7() { printf '\e]7;file://%s%s\e\\' "${HOST%%.*}" "${PWD// /%20}"; }
autoload -Uz add-zsh-hook 2>/dev/null
add-zsh-hook chpwd _emit_osc7 2>/dev/null
add-zsh-hook precmd _emit_osc7 2>/dev/null
_emit_osc7  # initial dir

# Auto-rename tmux session to basename of cwd (only from the first pane of the first window)
# REASON: Ghostty tabs show the tmux session name (via set-titles in tmux.conf).
# This keeps the session name in sync with the directory of the "primary" pane.
# The check runs once at shell startup; if panes are rearranged, the next session
# will pick up the correct pane via its own shell init.
#
# REASON for the _tmux_restore_in_progress gate: tmux-resurrect restores in two
# phases — it creates EVERY pane, then walks the save file again to send each pane
# its relaunch command, addressing panes by the session name recorded in the save
# file. Restored shells reach their first prompt during phase one, so without this
# gate they rename sessions out from under phase two and resurrect's
# pane_exists "<old name>" lookup fails, silently dropping the pane's agent (it
# returns 1, which is not even the "not auto-restored" echo path). That is how 10
# of 38 agent panes came back as bare shells after the 2026-08-10 reboot.
# ~/.tmux/resurrect-restore-guard.sh raises the flag before the restore and does
# the renaming itself once the relaunch commands have all been delivered.
# What breaks if removed: every reboot loses the agents in any session whose name
# does not already equal its cwd basename.
zmodload zsh/datetime 2>/dev/null
_tmux_restore_in_progress() {
  local stamp now
  stamp="$(tmux show-option -gqv '@resurrect-restore-in-progress' 2>/dev/null)"
  [[ "$stamp" == <-> ]] || return 1
  # REASON: fall back to date(1) if zsh/datetime is unavailable. Treating an unset
  # EPOCHSECONDS as 0 would make the staleness test below always true and freeze
  # session renaming permanently.
  now="${EPOCHSECONDS:-$(date +%s)}"
  # A stale flag (crashed restore) must not disable renaming forever.
  (( now - stamp < 300 ))
}

if [[ -n "$TMUX" ]]; then
  if [[ "$TMUX_PANE" == "$(tmux list-panes -t :^ -F '#{pane_id}' 2>/dev/null | head -1)" ]]; then
    _tmux_auto_rename_session() {
      _tmux_restore_in_progress && return
      tmux rename-session "${PWD##*/}" 2>/dev/null
    }
    precmd_functions+=(_tmux_auto_rename_session)
  fi
fi

# Auto-rename the tmux WINDOW to the basename of the current dir — from the ACTIVE pane only.
# REASON: tmux's pane_current_path / automatic-rename track the pane's ROOT process,
# which here is `secretty` (a wrapper that spawns this zsh as a child). secretty's cwd
# is frozen at wherever it launched (often /), so the window name showed "/" or the
# process name instead of where you actually are. This hook runs in the real interactive
# shell, so ${PWD:t} is the true current directory. rename-window also turns off
# automatic-rename for the window, so it won't fight this.
# REASON for the #{pane_active} gate: precmd fires in EVERY pane's shell, so without it a
# background pane (e.g. the "first" pane sitting in another dir) renames the shared window
# on its own prompt and clobbers the name out from under the pane you're actually in.
# Gating on the active pane makes the window name follow the CURRENT pane's dir.
# What breaks if removed: the window name flaps to whichever pane last drew a prompt
# instead of tracking the focused pane.
if [[ -n "$TMUX" ]]; then
  _tmux_auto_rename_window() {
    [[ "$(tmux display-message -p -t "$TMUX_PANE" '#{pane_active}' 2>/dev/null)" == 1 ]] \
      && tmux rename-window "${PWD:t}" 2>/dev/null
  }
  precmd_functions+=(_tmux_auto_rename_window)
fi
# REASON: cmux injects per-surface Claude/Codex wrapper shims, but earlier PATH
# prepends (especially the stable Claude TCC wrapper above) left the shim near
# the end of PATH. Claude then launched directly and cmux never received its
# SessionStart checkpoint, leaving the surface with no resume binding. Keep the
# per-surface shim first only inside a cmux terminal; the shim removes itself
# before it resolves the real agent binary, so this does not recurse.
if [[ -n "${CMUX_CLAUDE_WRAPPER_SHIM_ROOT:-}" && -x "${CMUX_CLAUDE_WRAPPER_SHIM:-}" ]]; then
  export PATH="${CMUX_CLAUDE_WRAPPER_SHIM_ROOT}:$PATH"
fi
