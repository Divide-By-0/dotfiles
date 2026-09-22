#!/bin/bash
# real-cwd.sh <pane_pid> — print the REAL working directory of a tmux pane.
#
# REASON: panes are wrapped by `secretty` (secretty -> zsh -> [bash ->] codex/...),
# so tmux's #{pane_current_path} reports secretty's cwd (frozen at /), not where the
# user actually is. tmux can't see through secretty, but `ps`/`lsof` on the live
# process tree can. This walks the pane's process subtree and returns the DEEPEST
# descendant whose cwd isn't "/" or a Claude/Codex plugin installation — that's
# the interactive shell's dir for a plain pane, or the agent's project dir (e.g.
# codex's node_repl) for an agent pane. Plugin servers are long-lived children of
# the agent but their package directory is not the pane's working directory.
# Falls back to $HOME if nothing better is found. Used by the new-window/split
# key bindings so they open in the dir you forked from, not /.
#
# Mirrors the real_cwd() logic in resurrect-secretty-fix.sh — keep them in sync.

pid="${1:-}"
printf '%s' "$pid" | grep -Eq '^[0-9]+$' || { printf '%s' "$HOME"; exit 0; }

cwd_of() { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | awk '/^n/{sub(/^n/,"");print;exit}'; }

is_plugin_cwd() {
  case "$1" in
    "$HOME/.claude/plugins"|"$HOME/.claude/plugins/"*|\
    "$HOME/.codex/plugins"|"$HOME/.codex/plugins/"*) return 0 ;;
    *) return 1 ;;
  esac
}

best=""; bestdepth=-1; queue="${pid}:0"
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
[ -n "$best" ] || best="$HOME"
printf '%s' "$best"
