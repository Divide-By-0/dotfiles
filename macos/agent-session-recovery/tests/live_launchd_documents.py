"""Opt-in live reproducer: run installed entry points inside a launchd tmux server.

Moshi tabs and cmux mirrors are spawned by the launchd-started tmux server
(com.aayush.tmux-autostart). launchd makes tmux its own TCC-responsible
process, and without a Documents/iCloud grant each open() under ~/Documents
blocks ~20 s and then fails with EINTR. This test submits a throwaway launchd
job that starts a private tmux server, runs each installed entry point in it,
and requires each one to finish quickly.

The `documents-grant` probe reports whether *any* pane may use a cwd inside
~/Documents. It fails until tmux itself has Full Disk Access, which only a
human can grant in System Settings; run with --skip-grant to check the
installer's part only.

    python3 tests/live_launchd_documents.py [--skip-grant]
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

HOME = Path.home()
TMUX = '/opt/homebrew/bin/tmux'
DEADLINE = 12  # seconds; TCC's approval watchdog fires at ~20 s, a healthy start is ~1 s.

PROBES = {
    'mirror': ['/opt/homebrew/bin/python3', str(HOME / '.claude/hooks/moshi-cmux-mirror.py'), '--help'],
    'groups': ['/opt/homebrew/bin/python3', str(HOME / '.claude/hooks/moshi-cmux-groups.py'), '--help'],
    # A PID with no process tree: this checks the script loads, not lsof reach.
    'real-cwd': [str(HOME / '.tmux/real-cwd.sh'), '999999999'],
    'cmux-wrapper': ['/usr/bin/env', 'CMUX_REAL_BIN=/usr/bin/true', str(HOME / '.local/bin/cmux'), 'version'],
    'documents-grant': ['/bin/ls', str(HOME / 'Documents/.projects.nosync')],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-grant', action='store_true')
    args = parser.parse_args()
    probes = {k: v for k, v in PROBES.items() if not (args.skip_grant and k == 'documents-grant')}
    out = Path(tempfile.mkdtemp(prefix='launchd-documents-'))
    socket = 'launchd-documents-' + uuid.uuid4().hex[:8]
    label = 'com.aayush.test.' + socket
    script = out / 'run.sh'
    lines = ['#!/bin/sh', 'cd /']
    for name, cmd in probes.items():
        lines.append(f'{shlex.join(cmd)} >/dev/null 2>{out / (name + ".err")}; echo $? >{out / (name + ".rc")}')
    lines.append(f'touch {out / "done"}; sleep 600')
    script.write_text('\n'.join(lines) + '\n')
    script.chmod(0o755)
    subprocess.run(['launchctl', 'submit', '-l', label, '--', TMUX, '-L', socket, '-f', '/dev/null',
                    'new-session', '-d', str(script)], check=True)
    try:
        start = time.monotonic()
        failures = []
        for name in probes:
            rc = out / (name + '.rc')
            while not rc.exists() and time.monotonic() - start < DEADLINE * len(probes) + 30:
                time.sleep(0.2)
            elapsed = time.monotonic() - start
            if not rc.exists():
                failures.append(f'{name}: still blocked after {elapsed:.0f}s')
                break  # later probes run serially behind the blocked one
            code = rc.read_text().strip()
            err = (out / (name + '.err')).read_text().strip().splitlines()[-1:] or ['']
            print(f'{name}: rc={code} t={elapsed:.1f}s {err[0]}')
            if code != '0' or elapsed > DEADLINE:
                failures.append(f'{name}: rc={code} after {elapsed:.1f}s {err[0]}')
            start = time.monotonic()
        if failures:
            print('FAIL\n  ' + '\n  '.join(failures))
            return 1
        print('PASS')
        return 0
    finally:
        subprocess.run([TMUX, '-L', socket, 'kill-server'], capture_output=True)
        subprocess.run(['launchctl', 'remove', label], capture_output=True)


if __name__ == '__main__':
    sys.exit(main())
