import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.sysctl = self.path / 'sysctl'
        self.calls = self.path / 'calls'
        self.command = self.path / 'command'
        self.command.write_text('#!/bin/sh\necho called >> "$PTY_TEST_CALLS"\n')
        self.command.chmod(0o755)
        self.env = dict(os.environ, PTY_SYSCTL_BIN=str(self.sysctl),
                        PTY_WAIT_ATTEMPTS='1', PTY_TEST_CALLS=str(self.calls))

    def fake_sysctl(self, body):
        self.sysctl.write_text('#!/bin/sh\n' + body + '\n')
        self.sysctl.chmod(0o755)

    def run_gate(self):
        return subprocess.run([str(ROOT / 'bin/wait-for-pty-capacity.sh'),
                               str(self.command)], env=self.env, capture_output=True)

    def test_ready_executes_command(self):
        self.fake_sysctl('echo 999')
        self.assertEqual(self.run_gate().returncode, 0)
        self.assertEqual(self.calls.read_text(), 'called\n')

    def test_low_or_unreadable_capacity_blocks_command(self):
        for body in ['echo 511', 'exit 1', 'echo invalid']:
            with self.subTest(body=body):
                self.fake_sysctl(body)
                self.assertNotEqual(self.run_gate().returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_waits_for_daemon(self):
        self.fake_sysctl('if [ -f "$PTY_TEST_CALLS.ready" ]; then echo 999; else touch "$PTY_TEST_CALLS.ready"; echo 511; fi')
        self.env['PTY_WAIT_ATTEMPTS'] = '2'
        self.assertEqual(self.run_gate().returncode, 0)
        self.assertTrue(self.calls.exists())

    def test_restore_does_not_call_tmux_before_capacity_ready(self):
        self.fake_sysctl('echo 511')
        self.env['TMUX_BIN'] = str(self.command)
        result = subprocess.run([str(ROOT / 'bin/tmux-autostart-restore.sh')],
                                env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_daemon_and_mirror_wiring(self):
        daemon = plistlib.loads((ROOT / 'launchdaemons/com.aayush.pty-capacity.plist').read_bytes())
        self.assertEqual(daemon['ProgramArguments'], ['/usr/sbin/sysctl', '-w', 'kern.tty.ptmx_max=999'])
        self.assertTrue(daemon['RunAtLoad'])
        mirror = plistlib.loads((ROOT / 'launchagents/com.aayush.moshi-cmux-groups.plist.in').read_bytes())
        self.assertEqual(mirror['ProgramArguments'][0], '__HOME__/.local/bin/wait-for-pty-capacity.sh')


if __name__ == '__main__':
    unittest.main()
