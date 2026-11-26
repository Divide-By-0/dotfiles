#!/usr/bin/env python3
"""Install only the private cmux checkpoint job; do not reload other services."""
import argparse
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
LABEL = "com.aayushgupta.cmux-checkpoints"


def install(home, activate=True):
    home = Path(home)
    state = home / ".local/state/cmux-checkpoints"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    for name in ("launchd.out", "launchd.err"):
        log = state / name
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    files = [(ROOT / "checkpoint.py", home / ".local/lib/cmux-checkpoint/checkpoint.py"),
             (ROOT / "bin/cmux-checkpoint", home / ".local/bin/cmux-checkpoint")]
    for src, dst in files:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.read_bytes() != src.read_bytes():
            shutil.copy2(dst, str(dst) + ".before-" + str(time.time_ns()))
        temp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, temp)
        temp.chmod(0o700)
        os.replace(temp, dst)
    plist = dict(Label=LABEL, ProgramArguments=[str(home / ".local/bin/cmux-checkpoint"), "capture"],
                 RunAtLoad=True, StartInterval=120, ProcessType="Background",
                 WorkingDirectory=str(home), ExitTimeOut=15,
                 EnvironmentVariables=dict(PATH=str(home / ".local/bin") + ":/opt/homebrew/bin:/usr/bin:/bin"),
                 StandardOutPath=str(state / "launchd.out"), StandardErrorPath=str(state / "launchd.err"))
    # capture prints just one short health record per run. launchd output logs
    # are rotated by the capture job so they cannot grow forever.
    path = home / "Library/LaunchAgents" / (LABEL + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(plist, stream)
    path.chmod(0o600)
    if activate:
        domain = "gui/" + str(os.getuid())
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        for attempt in range(10):
            result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        subprocess.run(["/bin/launchctl", "kickstart", domain + "/" + LABEL], check=True)
    print("Installed " + LABEL + " (every 120 seconds); state: " + str(state))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    install(args.home, not args.no_activate)
