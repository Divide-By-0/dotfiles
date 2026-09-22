"""Exercise real tmux-resurrect save/restore on an isolated tmux socket.

TMUX_RESURRECT_SOURCE points to a checkout of cff343cf9e81983d3da0c8562b01616f12e8d548.
RESURRECT_TEST_UNPATCHED=1 runs the same assertions against the original implementation.
The probe stands in for an agent: no real conversations or user server are touched.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("TMUX_RESURRECT_SOURCE", Path.home() / ".tmux/plugins/tmux-resurrect"))


class EmptyTitleRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resurrect-title-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.socket = str(self.root / "tmux.sock")
        self.plugin = self.root / "plugin"
        self.plugin.mkdir()
        # Use committed upstream files, never the user's locally patched plugin.
        archive = subprocess.check_output(["git", "-C", str(SOURCE), "archive", "HEAD"])
        subprocess.run(["tar", "-xf", "-", "-C", str(self.plugin)], input=archive, check=True)
        if not os.environ.get("RESURRECT_TEST_UNPATCHED"):
            subprocess.run(["git", "apply", str(ROOT / "patches/tmux-resurrect-empty-pane-title.patch")],
                           cwd=self.plugin, check=True)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "bootstrap", "/bin/sleep 120")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.socket, "kill-server"],
                                              capture_output=True))
        self.env = dict(os.environ, TMUX=f"{self.socket},{self.tmux('display-message', '-p', '#{pid}').strip()},0")
        self.tmux("set-option", "-g", "@resurrect-dir", str(self.root / "saved"))
        self.tmux("set-option", "-g", "default-shell", "/bin/bash")
        self.tmux("set-option", "-g", "default-command", "/bin/bash --noprofile --norc")
        self.tmux("set-option", "-g", "@resurrect-processes", '"~codex-recovery-probe"')
        self.tmux("set-option", "-g", "@resurrect-capture-pane-contents", "on")
        self.probe = self.root / "codex-recovery-probe"
        self.probe.write_text('#!/bin/bash\nprintf "started\\n" >> "$1"\nwhile :; do sleep 1; done\n')

    def tmux(self, *args):
        return subprocess.check_output(["tmux", "-S", self.socket, *args], stderr=subprocess.STDOUT).decode()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for the probe to restart: " + self.tmux("capture-pane", "-pJt", "project:0.0", "-S", "-40"))

    def exercise(self, title):
        marker = self.root / "starts"
        command = f"/bin/bash {shlex.quote(str(self.probe))} {shlex.quote(str(marker))}"
        # Keep a Bash parent between tmux and the simulated agent.
        wrapper = "/bin/bash -c " + shlex.quote(command + "; wait")
        self.tmux("new-session", "-d", "-s", "project", "-c", str(self.root), wrapper)
        self.wait_for(marker.exists)
        self.tmux("select-pane", "-t", "project:0.0", "-T", title)
        expected_pid = self.tmux("display-message", "-p", "-t", "project:0.0", "#{pane_pid}").strip()
        result = subprocess.run(["/bin/bash", "-x", str(self.plugin / "scripts/save.sh"), "quiet"],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split("\t") for line in (self.root / "saved/last").read_text().splitlines()]
        row = next(r for r in rows if r[:2] == ["pane", "project"])
        self.assertEqual(row[6], title or "(untitled)", row)
        self.assertEqual(row[7], ":" + str(self.root), row)
        self.assertEqual(row[8], "1", row)
        self.assertIn("codex-recovery-probe", row[10], row)
        self.assertIn(f"pane_full_command {expected_pid}", result.stderr)
        self.tmux("kill-session", "-t", "=project")
        subprocess.run(["/bin/bash", str(self.plugin / "scripts/restore.sh")], env=self.env,
                       capture_output=True, check=True, timeout=30)
        self.wait_for(lambda: marker.read_text().splitlines() == ["started", "started"])
        self.assertEqual(self.tmux("display-message", "-p", "-t", "project:0.0", "#{pane_current_path}").strip(), str(self.root))

    def test_empty_title_preserves_wrapped_command_and_restores(self):
        self.exercise("")

    def test_named_title_preserves_wrapped_command_and_restores(self):
        self.exercise("Existing project title")


if __name__ == "__main__":
    unittest.main()
