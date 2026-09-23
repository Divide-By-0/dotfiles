import json
import os
from pathlib import Path
import tempfile
import signal
import subprocess
import sys
import time
import unittest

from recorder import Capture


class RecorderTests(unittest.TestCase):
    def test_hard_stop_preserves_flushed_events_on_next_start(self):
        runner = (
            "import sys; from recorder import run; "
            "run(sys.argv[1], [sys.executable, '-u', '-c', "
            "\"import time; print('event-before-hard-stop', flush=True); time.sleep(60)\"])"
        )
        with tempfile.TemporaryDirectory() as tmp:
            def start():
                return subprocess.Popen([sys.executable, "-c", runner, tmp],
                                        cwd=Path(__file__).parent, start_new_session=True)

            def wait_checkpoint(expected_run=None):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        s = json.loads((Path(tmp) / "current/status.json").read_text())
                        if s["bytes_received"] > 0 and s["run_id"] != expected_run:
                            return s
                    except FileNotFoundError:
                        pass
                    time.sleep(0.05)
                self.fail("No flushed data checkpoint")

            p = start()
            try:
                before = wait_checkpoint()
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)
                p = start()
                wait_checkpoint(before["run_id"])
                archives = list((Path(tmp) / "incidents").iterdir())
                self.assertEqual(len(archives), 1)
                self.assertIn(b"event-before-hard-stop", (archives[0] / "000001.log").read_bytes())
            finally:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)

    def test_restart_preserves_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Capture(tmp)
            first.write(b"before crash\n")
            first.checkpoint()
            first.close()
            second = Capture(tmp)
            archives = list((Path(tmp) / "incidents").iterdir())
            self.assertEqual(len(archives), 1)
            self.assertEqual((archives[0] / "000001.log").read_bytes(), b"before crash\n")
            self.assertEqual(json.loads((archives[0] / "status.json").read_text())["state"], "running")
            self.assertFalse(list(second.current.glob("*.log")))
            second.close()

    def test_time_window_and_byte_cap_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Capture(tmp, window=300, active_limit=100, segment_limit=10)
            for t in range(0, 601, 60):
                c.write(b"1234567890", now=t)
            self.assertEqual(c.segments[0]["start"], 300)
            self.assertEqual(c.evicted_bytes, 0)
            c.active_limit = 25
            c.prune(600)
            self.assertEqual(len(c.segments), 2)
            self.assertGreater(c.evicted_bytes, 0)
            c.close()

    def test_history_cap_does_not_touch_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Capture(tmp, archive_limit=15)
            for name in ["001", "002", "003"]:
                p = c.archives / name
                p.mkdir()
                (p / "log").write_bytes(b"1234567890")
            c.write(b"current\n")
            c.prune_archives()
            self.assertEqual([p.name for p in c.archives.iterdir()], ["003"])
            self.assertEqual((c.current / "000001.log").read_bytes(), b"current\n")
            c.close()

    def test_duplicate_start_cannot_archive_live_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Capture(tmp)
            c.write(b"still running\n")
            with self.assertRaises(RuntimeError):
                Capture(tmp)
            self.assertFalse(list(c.archives.iterdir()))
            c.close()

    def test_private_permissions_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Capture(tmp)
            c.write(b"test\n")
            c.checkpoint()
            self.assertEqual(Path(tmp).stat().st_mode & 0o777, 0o700)
            for p in c.current.iterdir():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            state = json.loads((c.current / "status.json").read_text())
            self.assertEqual(state["bytes_received"], 5)
            c.close()


if __name__ == "__main__":
    unittest.main()
