#!/bin/sh
set -eu
# launchd does not order independent jobs. Check the kernel value itself,
# never a marker left over from a previous boot, before creating any panes.
SYSCTL_BIN="${PTY_SYSCTL_BIN:-/usr/sbin/sysctl}"
attempts="${PTY_WAIT_ATTEMPTS:-60}"
while [ "$attempts" -gt 0 ]; do
  capacity=$("$SYSCTL_BIN" -n kern.tty.ptmx_max 2>/dev/null || true)
  case "$capacity" in
    ''|*[!0-9]*) ;;
    *) if [ "$capacity" -ge 999 ]; then
         echo "PTY capacity ready: $capacity" >&2
         if [ "$#" -gt 0 ]; then exec "$@"; fi
         exit 0
       fi ;;
  esac
  attempts=$((attempts - 1))
  [ "$attempts" -eq 0 ] || sleep 1
done
echo "PTY capacity is not 999; refusing terminal startup. Install the com.aayush.pty-capacity LaunchDaemon." >&2
exit 1
