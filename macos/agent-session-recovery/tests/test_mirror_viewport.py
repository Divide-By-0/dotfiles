"""When the mirror resizes the desktop cmux terminal to the phone.

Every resize makes Claude Code reprint its whole conversation into the desktop
scrollback. On 2026-09-28 one tab held ~40 stacked copies at phone width
(36k lines, 844 distinct), which is what looked "cut off" when scrolling up in
cmux. So the phone width is held across swipes and released only when the
phone is gone or idle, instead of on every swipe.
"""
import importlib.util
import bdb
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

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


class Finished(Exception):
    pass


class Terminal(io.StringIO):
    def fileno(self):
        return 0


class LoopTrace(bdb.Bdb):
    """Capture actual main-loop locals at the scrollback/cleanup branches."""
    def __init__(self):
        super().__init__()
        self.observations = []

    def user_line(self, frame):
        if frame.f_code.co_name == 'main' and frame.f_locals.get('scroll_offset', 0):
            snapshot = {k: frame.f_locals.get(k) for k in
                        ('scroll_offset', 'scroll_dirty', 'viewport_surface', 'attached')}
            if not self.observations or snapshot != self.observations[-1]:
                self.observations.append(snapshot)


class LifecycleRun:
    """Run the real mirror loop against a deterministic expiring cmux viewport.

    Only OS I/O, time and the cmux RPC boundary are replaced. The model's five
    second lease makes missed renewals observable as actual geometry changes;
    the native smoke separately verifies cmux's real expiry/restoration contract.
    """
    def __init__(self, *, scroll=True, detach=12, stop=14, clear_failures=0,
                 resize_at=None, rebind_at=None, wall_jump_at=None, hide_at=None, hold_seconds=300,
                 reattach_at=None, stale_grid_until=None):
        self.now = 0.0
        self.detach, self.stop = detach, stop
        self.clear_failures = clear_failures
        self.resize_at, self.rebind_at = resize_at, rebind_at
        self.wall_jump_at = wall_jump_at
        self.hide_at, self.hold_seconds = hide_at, hold_seconds
        self.reattach_at = reattach_at
        self.stale_grid_until = stale_grid_until
        self.scroll = scroll
        self.wheel_sent = False
        self.leases = {}
        self.calls = []
        self.geometry = []
        self.frames = []
        self.output = Terminal()
        self.trace = LoopTrace()
        self.finished = False

    def size(self):
        return (52, 42) if self.resize_at is not None and self.now >= self.resize_at else (37, 29)

    def sleep(self, seconds):
        self.now += seconds
        self.record_geometry()
        if self.now >= self.stop and not self.finished:
            self.finished = True
            raise Finished()

    def record_geometry(self):
        for sid in ('A', 'B'):
            lease = self.leases.get(sid)
            size = lease[1] if lease and self.now - lease[0] < 5 else (156, 51)
            self.geometry.append((self.now, sid, size))

    def rpc(self, method, params):
        sid = params['surface_id']
        self.calls.append((self.now, method, dict(params)))
        if method == 'terminal.replay':
            if 'viewport_columns' in params:
                self.leases[sid] = (self.now, (params['viewport_columns'], params['viewport_rows']))
            size = (156, 51) if (self.stale_grid_until is not None and self.resize_at is not None
                                and self.resize_at <= self.now < self.stale_grid_until) else self.size()
            return {'render_grid': {'columns': size[0], 'rows': size[1],
                                   'render_epoch': sid, 'render_revision': 1, 'row_spans': []}}
        if method == 'terminal.viewport':
            if self.clear_failures:
                self.clear_failures -= 1
                return {'_error': 'transient cmux RPC failure'}
            self.leases.pop(sid, None)
            self.record_geometry()
            return {}
        if method == 'surface.read_text':
            return {'text': '\n'.join('line %d' % n for n in range(200))}
        raise AssertionError('unexpected RPC: ' + method)

    def select(self, *_):
        ready = self.scroll and not self.wheel_sent and self.now >= .25
        return ([0] if ready else [], [], [])

    def read(self, *_):
        self.wheel_sent = True
        return b'\x1b[<64;5;5M'

    def state(self, _):
        sid = 'B' if self.rebind_at is not None and self.now >= self.rebind_at else 'A'
        return {'surface_id': sid, 'grouped': True}

    def run(self):
        real_frame = mirror.scrollback_frame
        def frame(*args):
            self.frames.append((self.now, args))
            return real_frame(*args)
        def wall_time():
            return 1000 + self.now + (-3600 if self.wall_jump_at is not None and self.now >= self.wall_jump_at else 0)
        def phone_state(_):
            active = self.now < self.detach or (self.reattach_at is not None and self.now >= self.reattach_at)
            return active and (self.hide_at is None or self.now < self.hide_at), active
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            patches = [
             patch.object(mirror.sys, 'argv', ['mirror', tmp+'/state.json', tmp+'/queue']),
             patch.object(mirror.sys, 'stdin', Terminal()), patch.object(mirror.sys, 'stdout', self.output),
             patch.object(mirror.os, 'isatty', return_value=True),
             patch.object(mirror.termios, 'tcgetattr'), patch.object(mirror.termios, 'tcsetattr'),
             patch.object(mirror.tty, 'setraw'), patch.object(mirror.signal, 'signal'),
             patch.object(mirror, 'load_state', side_effect=self.state),
             patch.object(mirror, 'phone_state', side_effect=phone_state),
             patch.object(mirror, 'HOLD_SECONDS', self.hold_seconds),
             patch.object(mirror, 'term_size', side_effect=self.size),
             patch.object(mirror, 'rpc', side_effect=self.rpc), patch.object(mirror, 'log'),
             patch.object(mirror, 'drain_queue'), patch.object(mirror, 'refresh_names'),
             patch.object(mirror, 'scrollback_frame', side_effect=frame),
             patch.object(mirror.select, 'select', side_effect=self.select),
             patch.object(mirror.os, 'read', side_effect=self.read),
             patch.object(mirror.time, 'time', side_effect=wall_time),
             patch.object(mirror.time, 'monotonic', side_effect=lambda: 1000+self.now),
             patch.object(mirror.time, 'sleep', side_effect=self.sleep),
            ]
            for context in patches:
                stack.enter_context(context)
            try:
                self.trace.runcall(mirror.main)
            except Finished:
                pass
        return self


class MainLoopTests(unittest.TestCase):
    def test_reading_scrollback_keeps_phone_geometry_until_detach(self):
        run = LifecycleRun().run()
        self.assertTrue(run.trace.observations, 'debugger must reach real scrollback branch')
        desktop_jumps = [(t, size) for t, sid, size in run.geometry
                         if sid == 'A' and .5 < t < run.detach and size != (37, 29)]
        self.assertEqual(desktop_jumps, [], 'reading history must renew the phone viewport')

    def test_transient_clear_failure_is_retried_before_lease_expiry(self):
        run = LifecycleRun(scroll=False, detach=2, stop=4, clear_failures=1).run()
        restored = [t for t, sid, size in run.geometry if sid == 'A' and 2 < t < 4 and size == (156, 51)]
        self.assertTrue(restored, 'detach must retry a failed clear while the claim is still live')

    def test_rebind_retries_old_claim_without_clearing_new_surface(self):
        run = LifecycleRun(scroll=False, rebind_at=2, stop=4, clear_failures=1).run()
        restored = [t for t, sid, size in run.geometry if sid == 'A' and 2 < t < 4 and size == (156, 51)]
        self.assertTrue(restored, 'failed old-surface cleanup must survive a rebind')
        self.assertFalse(any(t < 4 and p['surface_id'] == 'B'
                             for t, m, p in run.calls if m == 'terminal.viewport'))

    def test_rotation_while_scrolled_updates_claim_and_repaints_history(self):
        run = LifecycleRun(resize_at=2, detach=6, stop=7).run()
        sizes = [size for t, sid, size in run.geometry if sid == 'A' and 3 < t < 6]
        self.assertTrue(sizes)
        self.assertEqual(set(sizes), {(52, 42)})
        self.assertTrue(any(t >= 2 and args[1:3] == (52, 42) for t, args in run.frames),
                        'history must repaint at the new phone dimensions')

    def test_exit_retries_transient_cleanup_failure(self):
        run = LifecycleRun(scroll=False, detach=99, stop=2, clear_failures=1).run()
        self.assertNotIn('A', run.leases, 'normal exit should retry cleanup before falling back to expiry')

    def test_clock_correction_does_not_extend_background_hold(self):
        run = LifecycleRun(scroll=False, detach=99, hide_at=2, stop=7,
                           wall_jump_at=2, hold_seconds=2).run()
        self.assertTrue(any(4 <= t < 7 and sid == 'A' and size == (156, 51)
                            for t, sid, size in run.geometry),
                        'hold expiry must use elapsed time, not an adjustable wall clock')

    def test_rebind_while_scrolled_starts_new_surface_live(self):
        run = LifecycleRun(rebind_at=2, stop=4).run()
        self.assertTrue(any(m == 'terminal.replay' and p['surface_id'] == 'B'
                            for t, m, p in run.calls), 'new surface must not inherit the old scroll mode')

    def test_reconnect_cancels_failed_detach_clear_before_retry(self):
        run = LifecycleRun(scroll=False, detach=2, reattach_at=3, stop=5, clear_failures=1).run()
        attempts = [t for t, m, p in run.calls if m == 'terminal.viewport' and t < 5]
        self.assertEqual(attempts, [2.0], 'reconnect must not clear and resize the same surface again')

    def test_history_waits_for_native_reflow_after_rotation(self):
        run = LifecycleRun(resize_at=2, stale_grid_until=3, detach=6, stop=7).run()
        frames = [(t, args) for t, args in run.frames if args[1:3] == (52, 42)]
        self.assertTrue(frames)
        self.assertTrue(all(t >= 3 for t, _ in frames), 'do not paint old-width history while cmux is resizing')


class LeaseIsolationTests(unittest.TestCase):
    def test_return_to_surface_cancels_failed_clear(self):
        lease = mirror.ViewportLease()
        with patch.object(mirror, 'replay_viewport', return_value={}), \
             patch.object(mirror, 'clear_viewport', side_effect=[False, True]) as clear:
            lease.renew('A', 37, 29, 0)
            lease.release()
            lease.retry_cleanup(1)
            lease.renew('A', 37, 29, 2)
            lease.retry_cleanup(3)
            self.assertEqual(clear.call_count, 1, 'retry must not clear a newly reacquired viewport')
            lease.close()
            self.assertEqual(clear.call_count, 2)

    def test_unavailable_cmux_has_bounded_exit_retries(self):
        lease = mirror.ViewportLease()
        with patch.object(mirror, 'replay_viewport', return_value={'_error': 'lost response'}), \
             patch.object(mirror, 'clear_viewport', return_value=False) as clear, \
             patch.object(mirror.time, 'sleep'):
            lease.renew('A', 37, 29, 0)
            lease.close()
            self.assertEqual(clear.call_count, 3)
            self.assertIn('A', lease.pending, 'unacknowledged cleanup must remain observable')


if __name__ == '__main__':
    unittest.main()
