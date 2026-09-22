#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TIMESTAMP=$(date "+%Y%m%dT%H%M%S")

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer configures macOS launchd and tmux." >&2
  exit 1
fi

if [ "$(/usr/sbin/sysctl -n kern.tty.ptmx_max)" -lt 999 ]; then
  echo "First run: sudo $ROOT/install-pty-capacity.sh" >&2
  exit 1
fi

backup_and_link() {
  src="$1"
  dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    return 0
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    backup="${dst}.before-agent-session-recovery.${TIMESTAMP}"
    mv "$dst" "$backup"
    echo "backed up $dst -> $backup"
  fi
  ln -s "$src" "$dst"
  echo "linked $dst -> $src"
}

for name in wait-for-pty-capacity.sh cmux tmux-autostart-restore.sh tmux-daily-resurrect-save.sh tmux-periodic-resurrect-save.sh agent-session-doctor; do
  backup_and_link "$ROOT/bin/$name" "$HOME/.local/bin/$name"
done

for name in tmux-resume-pane.sh resurrect-restore-guard.sh resurrect-secretty-fix.sh real-cwd.sh agent-window-name.sh reconcile-moshi-sessions.py; do
  backup_and_link "$ROOT/tmux/$name" "$HOME/.tmux/$name"
done

for name in moshi-claude-hook.sh moshi-cmux-groups.py moshi-cmux-mirror.py moshi-cmux-title.py moshi-ghost-keeper.sh tmux-pane-session.sh; do
  backup_and_link "$ROOT/claude-hooks/$name" "$HOME/.claude/hooks/$name"
done

plugin_root="$HOME/.tmux/plugins/tmux-resurrect"
plugin_patch="$ROOT/patches/tmux-resurrect-tmux-3.7-session-targets.patch"
if [ ! -d "$plugin_root/.git" ]; then
  echo "tmux-resurrect is not installed at $plugin_root; install it with TPM first." >&2
  exit 1
fi
if git -C "$plugin_root" apply --reverse --check "$plugin_patch" >/dev/null 2>&1; then
  echo "tmux-resurrect compatibility patch already applied"
elif git -C "$plugin_root" apply --check "$plugin_patch"; then
  git -C "$plugin_root" apply "$plugin_patch"
  echo "applied tmux 3.7 literal-session-target patch"
else
  echo "tmux-resurrect changed upstream; refusing to force the compatibility patch." >&2
  exit 1
fi

ensure_tmux_source() {
  conf="$HOME/.tmux.conf"
  line="source-file \"$ROOT/config/tmux-session-recovery.conf\""
  touch "$conf"
  grep -Fqx "$line" "$conf" 2>/dev/null && return 0
  tmp=$(mktemp "${TMPDIR:-/tmp}/tmux-conf.XXXXXX")
  awk -v line="$line" '
    !inserted && /tmux\/plugins\/tpm\/tpm/ { print line; print ""; inserted=1 }
    { print }
    END { if (!inserted) print line }
  ' "$conf" >"$tmp"
  mv "$tmp" "$conf"
  echo "sourced tracked tmux recovery config from $conf"
}

ensure_zsh_source() {
  conf="$HOME/.zshrc"
  line="source \"$ROOT/config/zsh-session-recovery.zsh\""
  touch "$conf"
  grep -Fqx "$line" "$conf" 2>/dev/null && return 0
  printf "\n%s\n" "$line" >>"$conf"
  echo "sourced tracked shell recovery config from $conf"
}

render_plist() {
  template="$1"
  dst="$2"
  tmp=$(mktemp "${TMPDIR:-/tmp}/launchagent.XXXXXX")
  sed "s|__HOME__|$HOME|g" "$template" >"$tmp"
  plutil -lint "$tmp" >/dev/null
  if [ -e "$dst" ] && ! cmp -s "$tmp" "$dst"; then
    cp -p "$dst" "${dst}.before-agent-session-recovery.${TIMESTAMP}"
  fi
  mv "$tmp" "$dst"
  echo "installed $dst"
}

ensure_tmux_source
ensure_zsh_source
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
render_plist "$ROOT/launchagents/com.aayush.tmux-autostart.plist.in" "$HOME/Library/LaunchAgents/com.aayush.tmux-autostart.plist"
render_plist "$ROOT/launchagents/com.aayush.tmux-daily-resurrect-save.plist.in" "$HOME/Library/LaunchAgents/com.aayush.tmux-daily-resurrect-save.plist"
render_plist "$ROOT/launchagents/com.aayush.tmux-periodic-resurrect-save.plist.in" "$HOME/Library/LaunchAgents/com.aayush.tmux-periodic-resurrect-save.plist"

render_plist "$ROOT/launchagents/com.aayush.moshi-cmux-groups.plist.in" "$HOME/Library/LaunchAgents/com.aayush.moshi-cmux-groups.plist"

if [ "${1:-}" = "--activate" ]; then
  domain="gui/$(id -u)"
  for label in com.aayush.tmux-autostart com.aayush.tmux-daily-resurrect-save com.aayush.tmux-periodic-resurrect-save com.aayush.moshi-cmux-groups; do
    if [ "$label" = "com.aayush.tmux-autostart" ] && launchctl print "$domain/$label" >/dev/null 2>&1; then
      # Reloading a RunAtLoad restore job while tmux is live would restore the
      # same snapshot into the current server. The scripts are symlinked, so the
      # loaded job will use this version on the next login without a reload.
      if command -v tmux >/dev/null 2>&1 && tmux has-session >/dev/null 2>&1; then
        server_pid=$(tmux display-message -p '#{pid}')
        tmux set-option -g @agent-session-boot-restore-state "$server_pid:complete"
      fi
      echo "left active $label loaded; current tmux server marked restored"
      continue
    fi
    if [ "$label" = "com.aayush.tmux-autostart" ] && command -v tmux >/dev/null 2>&1 && tmux has-session >/dev/null 2>&1; then
      # A first install into an already-running tmux server should preserve that
      # live state, not merge a historical snapshot into it.
      server_pid=$(tmux display-message -p '#{pid}')
      tmux set-option -g @agent-session-boot-restore-state "$server_pid:complete"
    fi
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "$domain" "$HOME/Library/LaunchAgents/$label.plist"
  done
  echo "LaunchAgents loaded"
else
  echo "files installed; run $0 --activate to reload the LaunchAgents"
fi
