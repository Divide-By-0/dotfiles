"""Installed runtime files must not resolve back into this checkout.

This checkout lives under ~/Documents, which macOS gates with TCC per
*responsible process*. The launchd-started tmux server (and every Moshi tab or
cmux mirror it spawns) has no grant there: each open() waits ~20 s on a TCC
approval that never arrives and then fails with EINTR ("Interrupted system
call"). Python retries EINTR on open() forever, so mirror panes hang empty.
Installed entry points and sourced config therefore have to be real copies.

Runs install.sh against a throwaway HOME with a stub tmux-resurrect checkout.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path.home() / '.tmux/plugins/tmux-resurrect'


class InstallOutsideDocumentsTests(unittest.TestCase):
    def setUp(self):
        if not (PLUGIN / '.git').is_dir():
            self.skipTest('tmux-resurrect checkout needed for the patch fixture')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        plugin = self.home / '.tmux/plugins/tmux-resurrect'
        (plugin / 'scripts').mkdir(parents=True)
        restore = subprocess.run(['git', '-C', str(PLUGIN), 'show', 'HEAD:scripts/restore.sh'],
                                 check=True, capture_output=True).stdout
        (plugin / 'scripts/restore.sh').write_bytes(restore)
        git = ['git', '-C', str(plugin), '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(git + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', 'fixture'], check=True)
        env = dict(os.environ, HOME=str(self.home))
        subprocess.run([str(ROOT / 'install.sh')], env=env, check=True, capture_output=True)

    def installed(self):
        for sub in ['.local/bin', '.tmux', '.claude/hooks', '.config']:
            base = self.home / sub
            if not base.exists():
                continue
            for path in base.rglob('*'):
                if 'plugins' in path.parts:
                    continue
                yield path

    def test_no_installed_file_resolves_into_checkout(self):
        leaks = [str(p) for p in self.installed()
                 if p.resolve().is_relative_to(ROOT.resolve())]
        self.assertEqual(leaks, [])

    def test_entry_points_are_executable_copies(self):
        for rel in ['.local/bin/cmux', '.tmux/real-cwd.sh', '.claude/hooks/moshi-cmux-mirror.py',
                    '.claude/hooks/moshi-cmux-groups.py']:
            with self.subTest(rel=rel):
                path = self.home / rel
                self.assertFalse(path.is_symlink())
                self.assertTrue(os.access(path, os.X_OK) or path.suffix == '.py')

    def test_shell_and_tmux_config_do_not_source_checkout(self):
        for rc in ['.tmux.conf', '.zshrc']:
            with self.subTest(rc=rc):
                self.assertNotIn(str(ROOT), (self.home / rc).read_text())

    def test_reinstall_replaces_legacy_checkout_source_lines(self):
        legacy_tmux = f'source-file "{ROOT}/config/tmux-session-recovery.conf"\n'
        legacy_zsh = f'source "{ROOT}/config/zsh-session-recovery.zsh"\n'
        (self.home / '.tmux.conf').write_text(legacy_tmux)
        (self.home / '.zshrc').write_text(legacy_zsh)
        env = dict(os.environ, HOME=str(self.home))
        subprocess.run([str(ROOT / 'install.sh')], env=env, check=True, capture_output=True)
        self.test_shell_and_tmux_config_do_not_source_checkout()
        self.assertIn('agent-session-recovery/tmux-session-recovery.conf',
                      (self.home / '.tmux.conf').read_text())


if __name__ == '__main__':
    unittest.main()
