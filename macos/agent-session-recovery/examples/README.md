# Minimal grouped-tab example

Download **`moshi-cmux-tabs.py`** and run it from a cmux terminal:

```sh
python3 moshi-cmux-tabs.py
```

Requirements: cmux, tmux, Python 3, and an already-connected Moshi installation.
No Python packages or other repository files are needed. Open the printed
`cmux-demo-...` session in Moshi's tmux session picker. Its windows mirror the
terminal tabs in that horizontal tab strip, in their current order. Prefix
`Ctrl-b` then `n` / `p` switches between them. Moshi's tmux swipe uses those
commands; the phone UI itself has not been tested.

The example snapshots one tab strip. It does not sync later tab changes or
install notification hooks, background services, or agent processes. Display
is plain text with basic keyboard input; colors, mouse, wide-character layout,
and viewport resizing are intentionally omitted. It leaves desktop focus alone.
Reruns create separate demo sessions; remove an old demo with:

```sh
tmux kill-session -t '=cmux-demo-<printed-id>'
```

This closes only the mirrors, not the original cmux terminals. Tested with
cmux 0.64.22 and moshi-hook 0.3.19: two real cmux shells, tmux next/previous,
command input/output round trips, and `moshi context`. Automated coverage runs
in CI (`tests/test_moshi_example.py`).

---

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
