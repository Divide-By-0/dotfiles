#!/usr/bin/env python3
"""Failure-focused checks: disabled restore, PID reuse, partial restart, lost RPC."""
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("checkpoints", ROOT / "checkpoint.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SURFACE = "33333333-3333-4333-8333-333333333333"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.app = cp.Checkpoints(home=self.home, state=self.home / "state", cmux="stub-cmux")
        self.app.history.mkdir(parents=True)
        self.cwd = self.home / "directory with spaces; $(touch dangerous)"
        self.cwd.mkdir()
        self.history = self.home / ".codex/sessions/2026/11/25" / ("rollout-" + SID + ".jsonl")
        self.history.parent.mkdir(parents=True)
        self.history.write_text(json.dumps(dict(type="session_meta", payload=dict(id=SID))) + "\n")
        self.snapshot = dict(version=1, windows=[dict(tabManager=dict(workspaces=[dict(
            workspaceId="workspace-1", customTitle="workspace", panels=[dict(
                id=SURFACE, title="task", ttyName="ttys1", type="terminal", terminal=dict(
                    workingDirectory=str(self.cwd), resumeBinding=dict(kind="codex", checkpointId=SID,
                                                                        autoResume=False)))])]))])
        self.tree = dict(windows=[dict(workspaces=[dict(id="workspace-1", title="workspace", panes=[dict(
            surfaces=[dict(id=SURFACE, ref="surface:1", title="task", type="terminal", tty="ttys1")])])])])
        self.process = dict(pid=42, ppid=1, tty="ttys9", started=100, executable="/bin/codex",
                            argv=["/bin/codex", "resume", SID], surface_id=SURFACE)
        self.saved = dict(provider="codex", session_id=SID, cwd=str(self.cwd), transcript_path=str(self.history),
                          title="task", history_available=True, bypass_permissions=False,
                          saved_auto_resume=False, surface_id=SURFACE)

    def data(self, generation=None, sessions=None):
        return dict(schema=1, captured_at=time.time(), app_generation=generation or [[1, 100]],
                    snapshot_sha256="hash", sessions=[dict(self.saved)] if sessions is None else sessions,
                    dormant=[], unknown=[], terminal_count=1)

    def checkpoint(self, name, data):
        path = self.app.history / name
        path.mkdir()
        cp.atomic_json(path / "manifest.json", data)
        (path / "layout.json.gz").write_bytes(gzip.compress(b'{"windows":[]}'))
        return path

    def collect(self, observed=None, generation=None):
        return json.dumps(self.snapshot).encode(), self.data(generation), observed or set()

    def test_disabled_binding_is_captured_by_native_id_across_nested_pty(self):
        data, observed = cp.inventory(self.home, self.snapshot, self.tree, {42: self.process}, {})
        self.assertEqual(data["sessions"][0]["session_id"], SID)
        self.assertFalse(data["sessions"][0]["saved_auto_resume"])
        self.assertEqual(observed, {("codex", SID)})
        self.assertTrue(data["sessions"][0]["history_available"])

    def test_pid_reuse_does_not_revive_stale_hook(self):
        process = dict(self.process, argv=["/bin/codex", "--yolo"], started=999)
        hook = dict(pid=42, pidStartSeconds=100, sessionId=SID, surfaceId=SURFACE)
        data, _ = cp.inventory(self.home, self.snapshot, self.tree, {42: process}, {"codex": [hook]})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["unknown"][0]["reason"], "native session ID not captured")

    def test_live_hook_matches_process_start_and_surface_after_move(self):
        process = dict(self.process, argv=["/bin/codex", "--yolo"], surface_id="old-surface")
        hook = dict(pid=42, pidStartSeconds=100, sessionId=SID, surfaceId=SURFACE,
                    cwd=str(self.cwd), transcriptPath=str(self.history))
        data, _ = cp.inventory(self.home, self.snapshot, self.tree, {42: process}, {"codex": [hook]})
        self.assertEqual(len(data["sessions"]), 1)

    def test_title_and_reused_tty_never_select_a_session(self):
        process = dict(self.process, tty="ttys1", argv=["/bin/codex", "--yolo"])
        process.pop("surface_id")
        data, observed = cp.inventory(self.home, self.snapshot, self.tree, {42: process}, {})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(observed, set())
        self.assertEqual(data["dormant"][0]["session_id"], SID)

    def test_ambiguous_native_ids_are_reported_not_chosen(self):
        process = dict(self.process, argv=["/bin/codex", "resume", SID, "resume", OTHER])
        data, _ = cp.inventory(self.home, self.snapshot, self.tree, {42: process}, {})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["unknown"][0]["reason"], "ambiguous identity")

    def test_prompt_text_cannot_masquerade_as_native_resume_arguments(self):
        self.assertEqual(cp.argv_identity("codex", ["codex", "please resume " + SID]), set())
        self.assertEqual(cp.argv_identity("claude", ["claude", "explain --resume " + SID]), set())

    def test_clear_or_switch_does_not_keep_initial_resume_identity_silently(self):
        hook = dict(pid=42, pidStartSeconds=100, sessionId=OTHER, surfaceId=SURFACE)
        data, _ = cp.inventory(self.home, self.snapshot, self.tree, {42: self.process}, {"codex": [hook]})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["unknown"][0]["native_session_ids"], [SID, OTHER])
        self.assertEqual(cp.argv_identity("claude", ["claude", "-r", SID]), {SID})

    def test_kernel_argument_reader_round_trips_real_process_without_environment(self):
        argv, surface = cp.native_arguments(os.getpid())
        self.assertTrue(argv)
        self.assertTrue(any("test_cmux_checkpoints" in arg for arg in argv))
        self.assertTrue(surface is None or cp.UUID.fullmatch(surface))

    def test_old_checkpoint_does_not_resurrect_exited_agent_by_default(self):
        data, _ = cp.inventory(self.home, self.snapshot, self.tree, {}, {})
        self.assertEqual(data["sessions"], [])
        self.assertEqual(len(data["dormant"]), 1)

    def test_history_identity_and_location_are_checked(self):
        self.history.write_text(json.dumps(dict(type="session_meta", payload=dict(id=OTHER))) + "\n")
        self.assertIsNone(cp.transcript_path(self.home, "codex", SID, str(self.history)))
        outside = self.home / "untrusted.jsonl"
        outside.write_text(json.dumps(dict(type="session_meta", payload=dict(id=SID))) + "\n")
        self.assertIsNone(cp.transcript_path(self.home, "codex", SID, str(outside)))

    def test_capture_is_atomic_private_and_round_trips_layout(self):
        with patch.object(self.app, "collect", return_value=self.collect()):
            result = self.app.capture()
        path = self.app.history / result["checkpoint"]
        self.assertEqual(json.loads(gzip.decompress((path / "layout.json.gz").read_bytes())), self.snapshot)
        self.assertEqual((path / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((path / "layout.json.gz").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.app.state.glob(".capture-*")), [])
        before = (self.app.state / "status.json").read_bytes()
        with patch.object(self.app, "collect", side_effect=RuntimeError("RPC unavailable")):
            with self.assertRaises(RuntimeError):
                self.app.capture()
        self.assertEqual((self.app.state / "status.json").read_bytes(), before)
        self.assertTrue(path.exists())

    def test_layout_export_verifies_checksum_and_never_overwrites(self):
        raw, data, observed = self.collect()
        data["snapshot_sha256"] = cp.hashlib.sha256(raw).hexdigest()
        with patch.object(self.app, "collect", return_value=(raw, data, observed)):
            result = self.app.capture()
        output = self.home / "layout-export.json"
        self.app.export_layout(result["checkpoint"], output)
        self.assertEqual(output.read_bytes(), raw)
        with self.assertRaises(FileExistsError):
            self.app.export_layout(result["checkpoint"], output)
        archive = self.app.history / result["checkpoint"] / "layout.json.gz"
        archive.write_bytes(gzip.compress(b'{}'))
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.app.export_layout(result["checkpoint"], self.home / "corrupt.json")

    def test_recovery_uses_prior_app_generation_not_partial_new_snapshot(self):
        old = self.checkpoint("20260101", self.data([[1, 100]]))
        self.checkpoint("20260102", self.data([[2, 200]], []))
        self.assertEqual(self.app.baseline(self.data([[2, 200]]))[0], old)
        # JSON roundtrip must not make equal process-generation lists unequal.
        self.assertEqual(self.app.baseline(json.loads(json.dumps(self.data([[1, 100]]))))[0], old)

    def test_running_sessions_are_not_launched_even_with_auto_disabled(self):
        self.checkpoint("baseline", self.data())
        calls = []
        with patch.object(self.app, "collect", return_value=self.collect({("codex", SID)})), \
             patch.object(self.app, "cli", side_effect=lambda *a: calls.append(a)):
            result = self.app.recovery(apply=True)
        self.assertEqual(result["sessions"][0]["action"], "already-running")
        self.assertEqual(calls, [])

    def test_unidentified_agent_in_original_tab_cannot_trigger_duplicate_resume(self):
        self.checkpoint("baseline", self.data())
        current = self.data(sessions=[])
        current["unknown"] = [dict(provider="codex", surface_id=SURFACE)]
        with patch.object(self.app, "collect", return_value=(b'{}', current, set())):
            result = self.app.recovery(apply=True)
        self.assertEqual(result["sessions"][0]["action"], "identity-unconfirmed-in-occupied-tab")

    def test_recovery_rechecks_liveness_before_opening_new_tab(self):
        self.checkpoint("baseline", self.data())
        calls = []
        with patch.object(self.app, "collect", side_effect=[self.collect(), self.collect({("codex", SID)})]), \
             patch.object(self.app, "resume_command", return_value="safe-native-command"), \
             patch.object(self.app, "cli", side_effect=lambda *a: calls.append(a)):
            result = self.app.recovery(apply=True)
        self.assertEqual(result["sessions"][0]["action"], "already-running")
        self.assertFalse(calls)

    def test_missing_history_is_explicit_and_never_replaced_with_latest(self):
        self.checkpoint("baseline", self.data())
        self.history.unlink()
        with patch.object(self.app, "collect", return_value=self.collect()):
            result = self.app.recovery()
        self.assertEqual(result["sessions"][0]["action"], "history-missing")

    def test_lost_rpc_response_reserves_launch_before_send(self):
        self.checkpoint("baseline", self.data())
        with patch.object(self.app, "collect", return_value=self.collect()), \
             patch.object(self.app, "resume_command", return_value="safe-native-command"), \
             patch.object(self.app, "cli", side_effect=RuntimeError("lost response")):
            with self.assertRaises(RuntimeError):
                self.app.recovery(apply=True)
            result = self.app.recovery()
        self.assertEqual(result["sessions"][0]["action"], "launch-pending")

    def test_recovery_creation_uses_structured_rpc_not_legacy_text_receipts(self):
        self.checkpoint("baseline", self.data())
        calls = []
        def cli(*args):
            calls.append(args)
            return dict(workspace_id="new-workspace")
        with patch.object(self.app, "collect", return_value=self.collect()), \
             patch.object(self.app, "resume_command", return_value="safe-native-command"), \
             patch.object(self.app, "cli", side_effect=cli):
            result = self.app.recovery(apply=True)
        self.assertEqual(result["sessions"][0]["action"], "launch-requested")
        self.assertEqual(calls[0][:2], ("rpc", "workspace.create"))
        self.assertEqual(json.loads(calls[0][2])["initial_input"], "safe-native-command\r")

    def test_safe_argv_replaces_arbitrary_snapshot_commands(self):
        saved = dict(self.saved, command="touch dangerous", environment={"EVIL": "value"})
        with patch.object(cp.Path, "is_file", return_value=True):
            command = self.app.resume_command(saved)
        self.assertNotIn("touch dangerous &&", command)
        self.assertIn("--no-daemon", command)
        self.assertIn(SID, command)
        cwd_word = shlex.split(command)[2]
        self.assertEqual(cwd_word, str(self.cwd))
        with self.assertRaises(ValueError):
            self.app.resume_command(dict(saved, session_id="$(touch dangerous)"))

    def test_retention_keeps_latest_hourly_daily_and_is_size_bounded(self):
        now = time.time()
        for index, age in enumerate([40 * 86400, 20 * 86400, 20 * 86400 + 1, 2 * 86400,
                                     2 * 86400 + 1, 5, 1]):
            data = self.data()
            data["captured_at"] = now - age
            self.checkpoint("%02d" % index, data)
        self.app.prune(now)
        self.assertEqual([p.name for p, _ in self.app.entries()], ["06", "05", "04", "02"])
        self.app.prune(now, max_bytes=1)
        self.assertEqual([p.name for p, _ in self.app.entries()], ["06"])

    def test_standalone_install_uses_copies_and_only_its_own_read_only_job(self):
        spec = importlib.util.spec_from_file_location("install_checkpoint", ROOT / "install-cmux-checkpoints.py")
        install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install)
        with contextlib.redirect_stdout(io.StringIO()):
            install.install(self.home, activate=False)
        plist = plistlib.loads((self.home / "Library/LaunchAgents" / (install.LABEL + ".plist")).read_bytes())
        self.assertEqual(plist["StartInterval"], 120)
        self.assertEqual(plist["ProgramArguments"][-1], "capture")
        self.assertTrue(plist["RunAtLoad"])
        runtime = self.home / ".local/lib/cmux-checkpoint/checkpoint.py"
        self.assertEqual(runtime.read_bytes(), (ROOT / "checkpoint.py").read_bytes())
        self.assertFalse(runtime.is_symlink())
        self.assertEqual((self.home / ".local/state/cmux-checkpoints").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
