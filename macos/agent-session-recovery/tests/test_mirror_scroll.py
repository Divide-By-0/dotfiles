"""Vertical scrolling in a Moshi mirror pane.

The mirror draws on the alternate screen, so tmux copy-mode had no history to
scroll. The mirror now takes wheel events itself and shows the cmux tab's own
scrollback. These tests cover the pure pieces: input parsing and window math.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

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


class ColorHistoryTests(unittest.TestCase):
    def grid(self):
        return {
            'columns': 12, 'rows': 3, 'history_rows': 4, 'scrollback_rows': 4,
            'terminal_background': '#1e1e1e', 'terminal_foreground': '#ffffff',
            'styles': [{'id': 0, 'foreground': '#cc372e', 'background': '#1e1e1e'}],
            'scrollback_spans': [
                {'row': i, 'column': 0, 'text': 'red-%d' % i, 'style_id': 0}
                for i in range(4)],
            'row_spans': [{'row': 0, 'column': 0, 'text': 'live'}],
            'cursor': {'visible': True, 'row': 1, 'column': 0},
        }

    def test_history_keeps_native_colors_and_row_order(self):
        def rpc(method, params):
            if method == 'terminal.replay':
                self.assertEqual(params['anchor'], 'screen')
                self.assertGreaterEqual(params['max_scrollback_rows'], 3)
                return {'render_grid': self.grid()}
            return {'text': 'red-0\nred-1\nred-2\nred-3\nlive\n\n'}
        with patch.object(mirror, 'rpc', side_effect=rpc):
            frame, offset = mirror.scrollback_frame('test', 12, 3, 3)
        self.assertEqual(offset, 3)
        self.assertIn('red-2', frame)
        self.assertIn('red-3', frame)
        self.assertNotIn('red-1', frame)
        self.assertIn('38;2;204;55;46', frame)
        self.assertIn('48;2;30;30;30', frame)

    def test_live_and_history_overwrite_without_blank_screen(self):
        with patch.object(mirror, 'rpc', return_value={'render_grid': self.grid()}):
            history, _ = mirror.scrollback_frame('test', 12, 3, 3)
        for frame in (history, mirror.build_frame(self.grid(), 12, 3)):
            self.assertNotIn('\x1b[J', frame)
            self.assertNotIn('\x1b[2J', frame)
            self.assertIn('\x1b[?2026h', frame)
            self.assertTrue(frame.endswith('\x1b[?2026l'))

    def test_live_default_background_is_applied_before_paint(self):
        frame = mirror.build_frame(self.grid(), 12, 3)
        self.assertIn('48;2;30;30;30', frame.split('red')[0])

    def test_transient_read_failure_preserves_history_position(self):
        with patch.object(mirror, 'rpc', return_value={'_error': 'timeout'}):
            frame, offset = mirror.scrollback_frame('test', 12, 3, 3)
        self.assertEqual((frame, offset), ('', 3))

    def test_old_server_plain_text_fallback_remains_scrollable(self):
        def rpc(method, params):
            if method == 'terminal.replay':
                return {'render_grid': {'row_spans': []}}
            return {'text': '\n'.join('line-%d' % i for i in range(10))}
        with patch.object(mirror, 'rpc', side_effect=rpc):
            frame, offset = mirror.scrollback_frame('test', 12, 3, 3)
        self.assertEqual(offset, 3)
        self.assertIn('line-5', frame)
        self.assertIn('line-6', frame)


if __name__ == '__main__':
    unittest.main()
