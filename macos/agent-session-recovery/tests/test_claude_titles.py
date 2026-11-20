"""Exercise handoff titles, manual names and the real OSC/controlling-tty path."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "claude-hooks/claude-terminal-title.py"
spec = importlib.util.spec_from_file_location("titles", HOOK)
titles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(titles)


class ClaudeTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.transcript = self.home / "session.jsonl"
        self.session = "e83f5e54-d2f8-454d-8ac7-07c65a498ca7"
        self.append("ai-title", aiTitle="Codex task 01a128ab airtable submission")

    def append(self, kind, **values):
        with self.transcript.open("a") as out:
            out.write(json.dumps(dict(type=kind, sessionId=self.session, **values)) + "\n")

    def state(self):
        return titles.read_titles(self.transcript, self.session, self.home / "cache")

    def test_handoff_names_running_provider_and_removes_opaque_id(self):
        self.assertEqual(titles.title_for(self.state()), "Airtable submission — Claude")

    def test_custom_rename_wins_over_later_generated_titles(self):
        self.state()
        self.append("custom-title", customTitle="My chosen name")
        self.append("ai-title", aiTitle="Another generated name")
        self.assertEqual(titles.title_for(self.state()), "My chosen name")

    def test_topic_can_still_legitimately_mention_codex(self):
        self.append("ai-title", aiTitle="Fix Codex authentication")
        self.assertEqual(titles.title_for(self.state()), "Fix Codex authentication — Claude")

    def test_missing_transcript_still_names_new_terminal_claude(self):
        self.assertEqual(titles.title_for({}), "Claude")

    def test_new_session_uses_first_human_prompt_without_model_calls(self):
        self.transcript.unlink()
        self.append("user", message={"content": "Prepare Airtable submission\nExtra details"}, origin={"kind": "human"})
        self.append("user", message={"content": "continue"}, origin={"kind": "human"})
        self.assertEqual(titles.title_for(self.state()), "Prepare Airtable submission — Claude")

    def test_detached_hook_resolves_exact_cmux_surface_not_focused_tab(self):
        raw = json.dumps({"terminals": [
            {"surface_id": "other", "last_known_workspace_id": "workspace", "tty": "ttys002"},
            {"surface_id": "surface", "last_known_workspace_id": "workspace", "tty": "ttys166"}]})
        with patch.dict(os.environ, CMUX_SURFACE_ID="surface", CMUX_WORKSPACE_ID="workspace"), patch.object(titles.subprocess, "check_output", return_value=raw), patch.object(titles.os, "open", side_effect=[OSError(), 123]) as opened:
            self.assertEqual(titles.terminal_fd(), 123)
            self.assertEqual(opened.call_args.args[0], "/dev/ttys166")

    def test_control_sequences_and_bidi_cannot_escape_title(self):
        name = titles.title_for({"customTitle": "hello\x1b]2;evil\x07\n\u202eworld"})
        self.assertFalse(any(ord(c) < 32 or c == "\u202e" for c in name))

    def test_unfinished_metadata_line_is_retried_and_other_session_is_ignored(self):
        self.state()
        with self.transcript.open("a") as out:
            out.write('{"type":"custom-title","sessionId":"wrong","customTitle":"Wrong"}\n')
            out.write(json.dumps(dict(type="custom-title", sessionId=self.session, customTitle="New name")))
        self.assertEqual(titles.title_for(self.state()), "Airtable submission — Claude")
        with self.transcript.open("a") as out:
            out.write("\n")
        self.assertEqual(titles.title_for(self.state()), "New name")

    @unittest.skipIf(sys.platform == "darwin", "Native cmux live validation covers macOS; Linux CI exercises the isolated controlling tty")
    def test_real_hook_writes_osc_to_controlling_tty_and_nothing_to_stdout(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        launcher = ("import os,sys,fcntl,termios; "
                    "fd=os.open(sys.argv[1],os.O_RDWR); "
                    "fcntl.ioctl(fd,termios.TIOCSCTTY,0); "
                    "os.execv(sys.executable,[sys.executable,sys.argv[2]])")
        payload = json.dumps(dict(session_id=self.session, transcript_path=str(self.transcript)))
        process = subprocess.run([sys.executable, "-c", launcher, os.ttyname(slave), str(HOOK)], input=payload, text=True,
                                 capture_output=True, env=dict(os.environ, HOME=str(self.home)),
                                 start_new_session=True, timeout=5)
        self.assertEqual((process.returncode, process.stdout, process.stderr), (0, "", ""))
        self.assertTrue(select.select([master], [], [], 2)[0])
        self.assertEqual(os.read(master, 4096), "\x1b]2;Airtable submission — Claude\x07".encode())

    def test_installer_preserves_existing_hooks_settings_and_is_idempotent(self):
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {"EXISTING": "keep"}, "hooks": {"Stop": [
            {"hooks": [{"command": "existing-command", "type": "command"}]}]}, "model": "keep"}))
        env = dict(os.environ, HOME=str(self.home))
        for _ in range(2):
            subprocess.run([sys.executable, str(ROOT / "install-claude-titles.py")], env=env, check=True)
        data = json.loads(settings.read_text())
        self.assertEqual(data["env"], {"EXISTING": "keep", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"})
        self.assertEqual(data["model"], "keep")
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "existing-command")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertFalse((self.home / ".claude/hooks/claude-terminal-title.py").is_symlink())
        self.assertEqual(len(list(settings.parent.glob("settings.json.before-title-hook.*"))), 1)


if __name__ == "__main__":
    unittest.main()
