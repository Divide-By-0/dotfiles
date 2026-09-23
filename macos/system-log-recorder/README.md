# System log recorder

Keeps a private copy of `log stream --style compact --level info` independently
of macOS unified-log retention. Run `/usr/bin/python3 install.py` on macOS.
The installed script is copied into `~/.local/bin`, not symlinked into Documents.
The login LaunchAgent starts automatically and relaunches after recorder failure.
It does not capture the pre-login boot window, and does not need admin privileges.

## Finding the evidence

- `~/Library/Logs/SystemFlightRecorder/current/`: rolling capture and health status.
- `~/Library/Logs/SystemFlightRecorder/incidents/`: previous runs preserved at the
  next launch (including after reboot, logout, or recorder restart). These are
  candidate incidents, not proof a crash occurred. The directory name is the
  preservation time; `status.json` contains the original run and checkpoint times.
- `~/.local/bin/system-log-recorder --status`: last durable checkpoint. Check that
  `checkpoint_unix` is recent and `bytes_received` increases on an active system.
- To read a run, concatenate its numbered `.log` files in filename order. A chunk
  boundary can split a line between files; concatenation restores it.

Files are owner-only; nothing is uploaded, and private-value redaction is not
disabled. Logs may still contain sensitive application information.

## Durability and retention

The target is the last **15 minutes**, allowing margin around the requested five
minutes. Segments rotate every minute or 16 MiB. Unbuffered writes are committed
with fsync (and macOS F_FULLFSYNC) every five seconds. Checkpoint metadata is
atomically replaced. On the next start, the entire previous capture is renamed
into `incidents` before a new file is opened. An exclusive lock prevents a second
recorder from moving the live capture.

Active capture is capped at **512 MiB** plus at most one read chunk/segment;
archives are capped at **2 GiB / 30 days / 16 runs**, oldest first. Active size pruning is
reported as `bytes_evicted_early_by_size_cap`: an extreme log flood can shorten
the time window. The recorder needs five minutes of uptime to collect five
minutes. It cannot guarantee the final five seconds survive a hard power loss,
capture messages macOS never emits/drops, capture while logged out, or recover
logs from a crash before installation. Disk exhaustion can stop recording;
launchd retries, and `status.json` records errors when disk space permits.

This copies default/info/error/fault events exposed to the logged-in user, not
debug events or a full `.logarchive`. No speculative crash attribution or
automatic system changes are made. Application crashes without a logout do not
freeze the buffer: manually save `current` promptly if investigating one.

## Validation and removal

Run `/usr/bin/python3 -m unittest discover -s macos/system-log-recorder` from the
repository root. Tests cover previous-run preservation, time/size retention,
archive eviction, exclusive ownership, and file permissions. Installation should
also be checked with a real stream and a controlled recorder restart; do not
reboot the machine to test.

Stop with `launchctl bootout gui/$(id -u)/com.aayush.system-log-recorder`.
Remove `~/Library/LaunchAgents/com.aayush.system-log-recorder.plist` to disable
future login starts. Saved logs are left intact.
