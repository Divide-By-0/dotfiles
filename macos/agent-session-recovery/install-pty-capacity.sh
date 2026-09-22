#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ "$(uname -s)" = Darwin ] || { echo "macOS required" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo to install the system PTY LaunchDaemon." >&2; exit 1; }
source_plist="$ROOT/launchdaemons/com.aayush.pty-capacity.plist"
dest=/Library/LaunchDaemons/com.aayush.pty-capacity.plist
/usr/bin/plutil -lint "$source_plist"
# Verify this OS accepts the value before installing the boot configuration.
/usr/sbin/sysctl -w kern.tty.ptmx_max=999
if [ -e "$dest" ] && ! cmp -s "$source_plist" "$dest"; then
  cp -p "$dest" "$dest.backup.$(date +%Y%m%dT%H%M%S)"
fi
/usr/bin/install -o root -g wheel -m 644 "$source_plist" "$dest"
/bin/launchctl bootout system/com.aayush.pty-capacity >/dev/null 2>&1 || true
/bin/launchctl bootstrap system "$dest"
[ "$(/usr/sbin/sysctl -n kern.tty.ptmx_max)" -eq 999 ]
echo "PTY capacity set to 999 now and installed for every boot."
