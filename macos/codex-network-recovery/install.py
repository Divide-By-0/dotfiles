#!/usr/bin/env python3
"""Install reviewed copies outside Documents; preserve a rollback copy."""
import datetime
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from recover import MIN_FILES

if sys.platform != 'darwin':
    raise SystemExit('macOS only')
source = Path(__file__).resolve().parent
runtime = Path.home() / '.local/share/codex-network-recovery'
venv = runtime / 'venv'
logs = Path.home() / '.local/state/codex-network-recovery'
agents = Path.home() / 'Library/LaunchAgents'
for folder in (runtime, logs, agents):
    folder.mkdir(parents=True, exist_ok=True)
uv = shutil.which('uv')
if not uv:
    raise SystemExit('Install uv first')
if not (venv / 'bin/python').exists():
    subprocess.run([uv, 'venv', str(venv)], check=True)
subprocess.run([uv, 'pip', 'install', '--python', str(venv / 'bin/python'),
                '-r', str(source / 'requirements.txt')], check=True)
stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
for name in ('recover.py', 'start-daemon.py', 'requirements.txt'):
    target = runtime / name
    if target.exists() and target.read_bytes() != (source / name).read_bytes():
        shutil.copy2(target, target.with_name(target.name + '.before-' + stamp))
    shutil.copy2(source / name, target)
python = str(venv / 'bin/python')
for suffix, script, interval in [('recover', 'recover.py', 60), ('start', 'start-daemon.py', 300)]:
    label = 'com.aayush.codex-network-' + suffix
    path = agents / (label + '.plist')
    spec = {'Label': label, 'ProgramArguments': [python, str(runtime / script)],
            'RunAtLoad': True, 'StartInterval': interval,
            'EnvironmentVariables': {'PATH': str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/bin:/bin'},
            'SoftResourceLimits': {'NumberOfFiles': MIN_FILES},
            'StandardOutPath': str(logs / (suffix + '.log')),
            'StandardErrorPath': str(logs / (suffix + '-error.log'))}
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.before-' + stamp))
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path.write_bytes(plistlib.dumps(spec))
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
print('Installed 60-second recovery and high-capacity missing-daemon starter. Running work is unchanged.')
