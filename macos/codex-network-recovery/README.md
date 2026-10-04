# Codex overnight recovery

A narrowly scoped macOS watchdog for `Fatal error: application network permission was revoked` (including remote-compaction errors containing that message).

## Evidence and cause

On 2026-10-04, multiple managed-daemon tasks failed simultaneously at 08:56:11Z and 10:33:44Z. The daemon logged `Too many open files (os error 24)` at both times while reading skills and session metadata. The launchd soft file limit was 256. The installed daemon was Codex 0.160.0.

The matching upstream [application-network source](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server/src/application_network.rs) revokes application network policy if loading local requirements fails (`load_local_network_policy` → `fail_application_policy`). Thus a file-resource failure can appear as permission revocation. This preserves a security boundary: the remedy increases file capacity and retries through normal policy loading; it never turns off the policy or grants network permissions.

## Install

Requires macOS, `uv`, and an installed `codex` command:

```sh
python3 macos/codex-network-recovery/install.py
```

The installer copies reviewed source outside Documents to `~/.local/share/codex-network-recovery`, installs its pinned WebSocket dependency in a dedicated venv, and registers two LaunchAgents:

- `com.aayush.codex-network-recover`: check every 60 seconds.
- `com.aayush.codex-network-start`: start a **missing** managed daemon every 300 seconds, with a soft file limit of at least 4096. A running daemon is left alone.

Both jobs have `SoftResourceLimits.NumberOfFiles=4096`, and the starter raises its own soft limit before invoking the normal daemon command. A daemon spawned by this starter inherits that capacity. A daemon started first by another application can still inherit that application's limit. An already-running daemon cannot inherit a later parent's limit: fixing that process requires a coordinated restart after its work finishes. On the installation machine, the separate updater was refreshed under the 4096 limit without restarting the daemon; upstream source confirms updater SIGTERM stops only its loop. This installer intentionally does not restart active tasks. On other machines, upstream automatic updates may spawn from an existing low-limit updater; the watcher continues to protect interrupted tasks, but does not claim to fix that binary's resource management.

## Recovery contract

Only non-archived tasks updated within 24 hours are examined, using the canonical ID/path from the read-only state database. The newest durable lifecycle event must be a completed turn with this exact error. A 30-second settling period gives existing owners time to resume it. The server must still show `systemError`, and the latest server turn must match the failed turn. Completed tasks, ordinary errors, user aborts, active tasks, and newer turns are excluded.

Top-level loaded tasks resume through the existing daemon's authenticated local control socket using `thread/resume` then `turn/start`. No model, effort, cwd, policy, permission, tool, or environment overrides are sent. Approval requests remain with the existing owner. The watcher closes its connection after the start acknowledgement.

For failed children, the watcher steers an **active** parent to resume a still-assigned child. It does not directly revive a child: the parent decides whether its publisher or other assignment was deliberately paused or retired. Parents waiting for input or approval are left alone. Unloaded threads and tasks owned by other app-server processes are not recovered by this daemon watcher.

There is at most one recovery submission per failed turn and three per task per hour. Intent is saved before dispatch, so an uncertain/lost reply is not resent. Logs contain IDs/outcomes, not prompts, credentials, or tool outputs. State and logs live under `~/.local/state/codex-network-recovery`. A local file lock prevents overlapping scans.

Dry run:

```sh
~/.local/share/codex-network-recovery/venv/bin/python ~/.local/share/codex-network-recovery/recover.py --dry-run
```

Stop recovery:

```sh
launchctl bootout gui/$(id -u)/com.aayush.codex-network-recover
launchctl bootout gui/$(id -u)/com.aayush.codex-network-start
```

## Validation

`python3 -m unittest discover -s macos/codex-network-recovery/tests -v`

Tests cover exact error matching, subsequent activity/abort exclusion, live statuses, child coordination, retry limits, uncertain delivery, unchanged settings, and a real isolated process that exhausts its 256-descriptor limit then successfully opens a file after applying the capacity fix. The exhaustion reproduction does not prove overnight app stability or replace a full upstream app-server debugger reproduction.
