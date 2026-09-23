#!/usr/bin/python3
"""Install a login LaunchAgent; deliberately copies out of protected Documents."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

LABEL = "com.aayush.system-log-recorder"


def main():
    home = Path.home()
    executable = home / ".local/bin/system-log-recorder"
    executable.parent.mkdir(parents=True, exist_ok=True)
    root = home / "Library/Logs/SystemFlightRecorder"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    service = "gui/%d/%s" % (os.getuid(), LABEL)
    subprocess.run(["/bin/launchctl", "bootout", service], capture_output=True)
    shutil.copyfile(Path(__file__).with_name("recorder.py"), executable)
    executable.chmod(0o700)
    with plist.open("wb") as handle:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/python3", str(executable)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "ProcessType": "Background",
            "Umask": 0o077,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }, handle)
    plist.chmod(0o600)
    subprocess.run(["/bin/launchctl", "enable", service], check=True)
    # bootout can return while launchd still holds the old job's teardown state.
    # Retry the same bootstrap briefly; do not unload other jobs or escalate.
    for attempt in range(15):
        result = subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            break
        if attempt == 14:
            raise RuntimeError("launchd bootstrap failed: " + result.stderr.strip())
        time.sleep(1)
    print("Installed " + str(executable))
    print("Private capture directory: " + str(root))


if __name__ == "__main__":
    main()
