"""When the mirror resizes the desktop cmux terminal to the phone.

Every resize makes Claude Code reprint its whole conversation into the desktop
scrollback. On 2026-09-28 one tab held ~40 stacked copies at phone width
(36k lines, 844 distinct), which is what looked "cut off" when scrolling up in
cmux. So the phone width is held across swipes and released only when the
phone is gone or idle, instead of on every swipe.
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mirror', ROOT / 'claude-hooks/moshi-cmux-mirror.py')
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class DecisionTests(unittest.TestCase):
    def test_visible_paints(self):
        self.assertEqual(mirror.viewport_action(1000, True, True, 1000), 'paint')

    def test_swiping_away_holds_phone_width(self):
        self.assertEqual(mirror.viewport_action(1000, False, True, 990), 'hold')

    def test_hold_expires(self):
        self.assertEqual(mirror.viewport_action(1000 + mirror.HOLD_SECONDS + 1, False, True, 1000), 'release')

    def test_no_phone_releases_at_once(self):
        self.assertEqual(mirror.viewport_action(1000, False, False, 999), 'release')

    def test_never_shown_releases(self):
        self.assertEqual(mirror.viewport_action(1000, False, True, None), 'release')


class PhoneStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sample = Path(self.tmp.name) / 'visibility.json'
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ['TMUX'] = 'test,1,0'
        os.environ['TMUX_PANE'] = '%2'

    def write(self, panes, activity):
        self.sample.write_text(json.dumps({'updated': time.time(), 'panes': panes, 'activity': activity}))

    def test_recent_input_on_this_pane_is_visible(self):
        self.write(['%2'], time.time() - 10)
        self.assertEqual(mirror.phone_state(self.sample), (True, True))

    def test_idle_phone_left_attached_is_not_visible(self):
        # Moshi backgrounded for hours keeps its tmux client attached.
        self.write(['%2'], time.time() - mirror.PHONE_IDLE_SECONDS - 5)
        self.assertEqual(mirror.phone_state(self.sample), (False, False))

    def test_other_pane_visible_phone_still_active(self):
        self.write(['%1'], time.time() - 10)
        self.assertEqual(mirror.phone_state(self.sample), (False, True))

    def test_no_clients(self):
        self.write([], 0)
        self.assertEqual(mirror.phone_state(self.sample), (False, False))


if __name__ == '__main__':
    unittest.main()
