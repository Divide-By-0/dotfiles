"""Exercise the installed entry points with launchd's restricted environment."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import shutil
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        hooks = self.home / '.claude/hooks'
        hooks.mkdir(parents=True)
        (hooks / 'moshi-cmux-groups.py').write_text(
            'import json,sys,os\nfrom pathlib import Path\n'
            'data={"args":sys.argv[1:],"input":sys.stdin.read()}\n'
            'if data["args"]==["--event"]: print(json.loads(data["input"]).get("hook_event_name", "")); sys.exit(0)\n'
            'Path(os.environ["HOME"],"launch.json").write_text(json.dumps(data))\n'
            'print(json.dumps(data))\n')
        bin_dir = self.home / '.local/bin'
        bin_dir.mkdir(parents=True)
        for name in ['wait-for-pty-capacity.sh', 'moshi-cmux']:
            source = ROOT / 'bin' / name
            if source.exists():
                target = bin_dir / name
                target.write_bytes(source.read_bytes())
                target.chmod(0o755)
        sysctl = self.home / 'sysctl'
        sysctl.write_text('#!/bin/sh\necho 999\n')
        sysctl.chmod(0o755)
        self.env = dict(HOME=str(self.home), PATH='/usr/bin:/bin',
                        PTY_SYSCTL_BIN=str(sysctl), PTY_WAIT_ATTEMPTS='1')

    def test_launchagent_starts_without_homebrew_python(self):
        template = (ROOT / 'launchagents/com.aayush.moshi-cmux-groups.plist.in').read_text()
        job = plistlib.loads(template.replace('__HOME__', str(self.home)).encode())
        result = subprocess.run(job['ProgramArguments'], input='', text=True,
                                capture_output=True, env=self.env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['args'], ['--watch'])

    def test_cmux_hook_uses_same_launcher_and_preserves_payload(self):
        payload = json.dumps({'hook_event_name': 'PreToolUse', 'session_id': 'fixture'})
        env = dict(self.env, CMUX_SURFACE_ID='fixture-surface')
        result = subprocess.run([str(ROOT / 'claude-hooks/moshi-claude-hook.sh')],
                                input=payload, text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'args': ['--hook'], 'input': payload + '\n'})

    def test_real_multiplexers_keep_local_hook_and_exit_status(self):
        moshi = self.home / 'moshi'
        moshi.write_text('#!/usr/bin/python3\nimport json,os,sys\n'
                         'print(json.dumps({"input":sys.stdin.read(),"disabled":os.environ.get("MOSHI_DISABLE_PARENT_TERMINAL_LOOKUP")}))\n'
                         'sys.exit(7)\n')
        moshi.chmod(0o755)
        payload = json.dumps({'hook_event_name': 'PreToolUse'})
        for terminal in [{}, {'TMUX_PANE': '%1'}, {'ZELLIJ': '1'}, {'HERDR_ENV': '1'}]:
            with self.subTest(terminal=terminal):
                env = dict(self.env, MOSHI_BIN=str(moshi), **terminal)
                if terminal:
                    env['CMUX_SURFACE_ID'] = 'fixture-surface'
                result = subprocess.run([str(ROOT / 'claude-hooks/moshi-claude-hook.sh')],
                                        input=payload, text=True, capture_output=True, env=env, timeout=5)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'input': payload + '\n', 'disabled': '1'})
                self.assertFalse((self.home / 'launch.json').exists())

    def test_tmux_new_window_hook_uses_shared_launcher(self):
        tmux = shutil.which('tmux')
        if not tmux:
            self.skipTest('tmux required')
        socket = 'moshi-launch-' + uuid.uuid4().hex
        command = [tmux, '-L', socket]
        self.addCleanup(lambda: subprocess.run(command + ['kill-server'], capture_output=True))
        hook = next(line for line in (ROOT / 'config/tmux-session-recovery.conf').read_text().splitlines()
                    if line.startswith("set-hook -g 'after-new-window[40]'"))
        config = self.home / 'tmux.conf'
        config.write_text(hook + '\n')
        subprocess.run(command + ['-f', str(config), 'new-session', '-d', '-s', 'cmux-fixture'],
                       env=self.env, check=True, capture_output=True)
        subprocess.run(command + ['new-window', '-d', '-t', 'cmux-fixture:'],
                       env=self.env, check=True, capture_output=True)
        receipt = self.home / 'launch.json'
        deadline = time.monotonic() + 5
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(receipt.exists(), 'new-window hook did not launch the bridge')
        self.assertEqual(json.loads(receipt.read_text())['args'], [])

    def test_runtime_removal_and_versioned_upgrade_are_discovered(self):
        runtimes = self.home / 'runtimes with spaces'
        runtimes.mkdir()
        candidate = runtimes / 'python3.14'
        candidate.write_text('#!/bin/sh\necho selected >> "$HOME/runtime-selected"\nexec /usr/bin/python3 "$@"\n')
        candidate.chmod(0o755)
        env = dict(self.env, MOSHI_RUNTIME_DIRS=str(runtimes))
        for version in ['python3.14', 'python3.15']:
            with self.subTest(version=version):
                if candidate.name != version:
                    candidate = candidate.rename(runtimes / version)
                result = subprocess.run([str(self.home / '.local/bin/moshi-cmux'), '--watch'],
                                        input='', text=True, capture_output=True, env=env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.home / 'runtime-selected').exists())
                (self.home / 'runtime-selected').unlink()
        candidate.unlink()
        result = subprocess.run([str(self.home / '.local/bin/moshi-cmux')],
                                input='', text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertIn('compatible Python', result.stderr)

    def test_broken_runtime_is_skipped_and_python_environment_is_isolated(self):
        runtimes = self.home / 'runtimes'
        runtimes.mkdir()
        broken = runtimes / 'python3'
        broken.write_text('#!/bin/sh\nexit 1\n')
        broken.chmod(0o755)
        fallback = runtimes / 'python3.12'
        fallback.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        fallback.chmod(0o755)
        poison = self.home / 'poison'
        poison.mkdir()
        (poison / 'json.py').write_text('raise RuntimeError("unrelated Python environment")\n')
        env = dict(self.env, MOSHI_RUNTIME_DIRS=str(runtimes), PYTHONPATH=str(poison), PYTHONHOME='/missing')
        result = subprocess.run([str(self.home / '.local/bin/moshi-cmux'), '--watch'],
                                input='', text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['args'], ['--watch'])

    def test_mirror_mode_uses_the_same_runtime_discovery(self):
        mirror = self.home / '.claude/hooks/moshi-cmux-mirror.py'
        mirror.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        result = subprocess.run([str(self.home / '.local/bin/moshi-cmux'), '--mirror', 'state with spaces', 'queue'],
                                input='', text=True, capture_output=True, env=self.env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['state with spaces', 'queue'])


if __name__ == '__main__':
    unittest.main()
