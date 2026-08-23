# Agent session recovery on macOS

This directory is the source of truth for the local cmux, tmux, Claude, Codex, Moshi, and launchd session-recovery glue. Installed files are symlinked back here where possible so fixes do not remain as untracked edits under the home directory.

## What it fixes

- cmux 0.64.x can type `cmux restore <agent> <checkpoint>` into the correct terminal and still reject it because the server cannot infer the current surface. `bin/cmux` verifies that the typed agent/checkpoint still matches the surface restore record, then uses the working `restore --surface` form.
- A login-time empty tmux server previously relied on tmux-continuum to restore in the background while the daily saver also ran at load. The save could win the race and replace `last` with an almost-empty snapshot. The LaunchAgent now runs one synchronous restore, checks the expected pane count, and logs a failure if the restore is incomplete. The daily saver runs only at 03:17 and keeps paired state/content archives.
- tmux 3.7 parses dots in bare targets such as `.projects.nosync` and `lxm.house` as pane separators. The tracked tmux-resurrect patch uses explicit `session:` targets so later windows are not silently dropped.
- Every restored Claude and Codex pane receives its own saved checkpoint ID. This prevents several panes in one directory from racing into the same most-recent conversation.
- Claude startup trust and large-session summary prompts are accepted only in panes being relaunched from an already saved checkpoint.

## Generated tmux names

Moshi needs a tmux pane through which it can mirror a cmux terminal. `moshi-cmux-title.py` names that helper session as:

```text
<sanitized cmux tab title>-<first 6 hex characters of the cmux surface id>
```

For example, `claude-task-a6c6a8` is a Moshi mirror for surface `A6C6A8...`, not a project name. A trailing `-x` is added when the desired name is already taken. These helpers are intentionally saved by tmux-resurrect; if their keeper process was already dead when the snapshot was made, they return as harmless stale shells.

Use the doctor to distinguish mirrors from ordinary sessions:

```sh
~/.local/bin/agent-session-doctor c20e58d9-c20e58 c20e58d9-c20e58-x cadgenbench
```

## Layout

- `bin/`: cmux compatibility shim, validated startup restore, daily saver, and session doctor.
- `tmux/`: per-pane agent resume, save/restore hooks, real-cwd resolution, and descriptive window naming.
- `claude-hooks/`: active cmux-aware Moshi bridge and the Claude pane/checkpoint registry.
- `config/`: snippets sourced by `.tmux.conf` and `.zshrc`.
- `launchagents/`: templates rendered with the current home directory.
- `patches/`: the small tmux-resurrect tmux 3.7 compatibility patch.
- `tests/`: syntax, fixture, patch-application, plist, and isolated tmux target tests.

## Dependencies

macOS, Homebrew tmux, TPM with `tmux-plugins/tmux-resurrect` and `tmux-continuum`, Claude Code, Codex, cmux, Moshi, Python 3, `plutil`, `lsof`, and `pgrep`.

Claude settings must invoke `~/.claude/hooks/moshi-claude-hook.sh` for the Moshi-forwarded lifecycle/tool events and invoke `~/.claude/hooks/tmux-pane-session.sh` on `SessionStart`. Existing settings are intentionally not overwritten because they may contain unrelated hooks and permissions.

## Install

```sh
cd macos/agent-session-recovery
./tests/run.sh
./install.sh --activate
tmux source-file ~/.tmux.conf
```

The installer backs up a differing installed file before replacing it, symlinks executable sources, applies the tmux-resurrect patch only when it applies cleanly, adds source lines to `.tmux.conf` and `.zshrc`, renders both LaunchAgents, and optionally reloads them. It never kills the running tmux server.

Logs:

- `/tmp/tmux-autostart.log` and `/tmp/tmux-autostart.err`
- `~/Library/Logs/tmux-daily-resurrect-save.log`
- `~/.claude/hooks/moshi-sessionstart.log`

## Validate after a restart

```sh
launchctl print gui/$(id -u)/com.aayush.tmux-autostart
tail -n 20 /tmp/tmux-autostart.log
tmux list-sessions
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_current_path}'
~/.local/bin/agent-session-doctor
```

A successful startup log reports `restore complete` with `live_after` greater than or equal to the snapshot's expected pane count.
