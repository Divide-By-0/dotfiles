# Agent session recovery on macOS

This directory is the source of truth for the local cmux, tmux, Claude, Codex, Moshi, and launchd session-recovery glue. Installed files are symlinked back here where possible so fixes do not remain as untracked edits under the home directory.

## What it fixes

- cmux 0.64.x can type `cmux restore <agent> <checkpoint>` into the correct terminal and still reject it because the server cannot infer the current surface. `bin/cmux` verifies that the typed agent/checkpoint still matches the surface restore record, then uses the working `restore --surface` form.
- A login-time empty tmux server previously relied on tmux-continuum to restore in the background while the daily saver also ran at load. The save could win the race and replace `last` with an almost-empty snapshot. The LaunchAgent now runs one synchronous restore, marks that tmux server as restored so LaunchAgent reloads cannot restore it twice, checks the expected pane count, and logs a failure if the restore is incomplete.
- A separate LaunchAgent saves every 15 minutes without relying on tmux-continuum's `status-right` interpolation, which terminal clients can erase. The 03:17 job still keeps paired daily state/content archives, and a shared lock prevents the periodic and daily jobs from overlapping.
- tmux 3.7 parses dots in bare targets such as `.projects.nosync` and `lxm.house` as pane separators. The tracked tmux-resurrect patch uses explicit `session:` targets so later windows are not silently dropped.
- Every restored Claude and Codex pane receives its own saved checkpoint ID. This prevents several panes in one directory from racing into the same most-recent conversation.
- Saved pane directories are taken from the live agent process tree, and Codex is resumed with `tui.resume_cwd=current`, so the checkpoint starts in the exact directory tmux restored rather than prompting or silently choosing another folder.
- Claude startup trust and large-session summary prompts are accepted only in panes being relaunched from an already saved checkpoint.
- Plain login-shell rows remain outside the auto-restore allowlist. Their visible `Previous command (not auto-restored)` annotation is intentional: it preserves evidence when a pre-reboot agent was accidentally saved as its outer login shell instead of silently treating that pane as successfully recovered.

## Generated tmux names

Moshi needs a tmux pane through which it can mirror a cmux terminal. These helper sessions use the human tab title plus the agent directory basename:

```text
moshi-<task>-<folder>
stale-moshi-<task>-<folder>
```

For example, an active helper may be `moshi-claude-task-quests`; after its keeper exits or it returns from a reboot without a live mirror, it becomes `stale-moshi-claude-task-quests`. Opaque cmux surface IDs are never used in new names. Name collisions use readable numeric suffixes such as `-2`. `reconcile-moshi-sessions.py` runs at restore completion and on Moshi session end, and only renames helpers—it does not kill or delete them.

Use the doctor to distinguish mirrors from ordinary sessions:

```sh
~/.local/bin/agent-session-doctor c20e58d9-c20e58 c20e58d9-c20e58-x cadgenbench
```

Verify every saved Claude/Codex pane against both its live process cwd and its checkpoint metadata:

```sh
~/.local/bin/agent-session-doctor --verify-cwds
```

A clean result ends with `mismatches=0`.

## Layout

- `bin/`: cmux compatibility shim, validated startup restore, periodic/daily savers, and session doctor.
- `tmux/`: per-pane agent resume, save/restore hooks, real-cwd resolution, descriptive window naming, and Moshi helper reconciliation.
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

The installer backs up a differing installed file before replacing it, symlinks executable sources, applies the tmux-resurrect patch only when it applies cleanly, adds source lines to `.tmux.conf` and `.zshrc`, renders all three LaunchAgents, and optionally reloads them. It never kills the running tmux server. Reloading the startup agent is safe: an already restored tmux server is marked and skipped.

Logs:

- `/tmp/tmux-autostart.log` and `/tmp/tmux-autostart.err`
- `~/Library/Logs/tmux-periodic-resurrect-save.log`
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
