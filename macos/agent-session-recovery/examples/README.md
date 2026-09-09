# Minimal cmux to Moshi reproducer

`moshi-cmux-mre.py` isolates the smallest working shape of the local bridge:

1. A Claude `SessionStart` event supplies `CMUX_SURFACE_ID` and
   `CMUX_WORKSPACE_ID`.
2. The hook creates one shadow tmux session for that cmux surface.
3. It replays the SessionStart payload from inside the shadow pane, which lets
   `moshi claude-hook` bind the agent to a supported terminal context.
4. The pane displays `terminal.replay` and sends input through
   `surface.send_text` or `surface.send_key`.

The deliberate reproducer is the session identity in step 2. Every surface is
a separate tmux session, so Moshi receives no relationship between surfaces
that belong to the same cmux workspace/vertical-tab set. The hook records the
workspace and surface IDs as tmux user options to show that the grouping data
exists, but that metadata is outside Moshi's current tmux context contract.

## Run it

Requirements: macOS, cmux, tmux, Moshi/moshi-hook, Python 3, and Claude Code.
The versions used for this repro were cmux 0.64.22 and moshi-hook 0.3.19.

Add this command alongside the normal Moshi command for Claude's
`SessionStart` hook, using the absolute path to the script:

```sh
python3 /absolute/path/to/moshi-cmux-mre.py hook
```

Start Claude in a cmux terminal. Moshi should show a session named
`moshi-cmux-mre-<surface-id>`. Attaching to it mirrors the cmux terminal and
basic typing, arrows, Enter, Tab, Backspace, Escape, and Ctrl-letter keys are
forwarded to the real surface.

To inspect the discarded grouping relationship:

```sh
tmux show-options -t 'moshi-cmux-mre-<surface-id>:' | grep cmux
```

Do not replace an existing Claude hook configuration just to run this demo.
Add the command as a second SessionStart hook or use a temporary Claude
configuration.

## Intentionally omitted

The production bridge has hook queuing, permission-result forwarding, ANSI
styles, wide-character rendering, title synchronization, idle polling,
reconnect handling, collision handling, and stale-session reconciliation.
None is needed to reproduce the successful Moshi attachment or the lost cmux
workspace hierarchy.

The full implementation is in `../claude-hooks/moshi-cmux-mirror.py` and
`../claude-hooks/moshi-claude-hook.sh`.
