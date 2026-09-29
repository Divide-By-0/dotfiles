"""Vertical scrolling in a Moshi mirror pane.

The mirror draws on the alternate screen, so tmux copy-mode had no history to
scroll. The mirror now takes wheel events itself and shows the cmux tab's own
scrollback. These tests cover the pure pieces: input parsing and window math.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mirror', ROOT / 'claude-hooks/moshi-cmux-mirror.py')
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class ParseTests(unittest.TestCase):
    def keys(self, data):
        return mirror.read_keys(bytearray(data))

    def test_sgr_wheel_is_one_whole_key(self):
        # Before: sequences were cut at 8 bytes and "30M" was typed into cmux.
        self.assertEqual(self.keys(b'\x1b[<64;12;30M'), [b'\x1b[<64;12;30M'])
        self.assertEqual(mirror.mouse_event(b'\x1b[<64;12;30M'), 'up')
        self.assertEqual(mirror.mouse_event(b'\x1b[<65;1;1M'), 'down')

    def test_other_mouse_events_are_swallowed_not_typed(self):
        for seq in [b'\x1b[<0;5;5M', b'\x1b[<0;5;5m', b'\x1b[<32;9;9M', b'\x1b[M !!']:
            with self.subTest(seq=seq):
                [key] = self.keys(seq)
                self.assertEqual(key, seq)
                self.assertEqual(mirror.mouse_event(key), 'other')

    def test_mixed_input_keeps_keys_and_mouse_apart(self):
        self.assertEqual(self.keys(b'a\x1b[<64;1;1M\x1b[Ab\r'),
                         [b'a', b'\x1b[<64;1;1M', b'\x1b[A', b'b', b'\r'])
        self.assertIsNone(mirror.mouse_event(b'\x1b[A'))
        self.assertIsNone(mirror.mouse_event(b'a'))

    def test_existing_keys_still_parse(self):
        self.assertEqual(self.keys(b'\x1b[5~\x1bOA\x1b[1;5C\x1b'),
                         [b'\x1b[5~', b'\x1bOA', b'\x1b[1;5C', b'\x1b'])


class WindowTests(unittest.TestCase):
    lines = [f'line {i}' for i in range(100)]

    def test_offset_shows_older_lines_above_status_row(self):
        visible, offset = mirror.scroll_window(self.lines, rows=10, offset=5)
        self.assertEqual(offset, 5)
        # rows-1 lines of content; the last row is the status bar.
        self.assertEqual(visible, [f'line {i}' for i in range(86, 95)])

    def test_offset_clamps_at_top_of_history(self):
        visible, offset = mirror.scroll_window(self.lines, rows=10, offset=500)
        self.assertEqual(offset, 91)
        self.assertEqual(visible[0], 'line 0')

    def test_short_history(self):
        visible, offset = mirror.scroll_window(['a', 'b'], rows=10, offset=3)
        self.assertEqual((visible, offset), (['a', 'b'], 0))


if __name__ == '__main__':
    unittest.main()
