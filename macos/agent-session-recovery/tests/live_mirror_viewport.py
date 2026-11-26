#!/usr/bin/env python3
"""Opt-in native cmux geometry checks; requires a disposable initialized shell.

Create a workspace named exactly 'Moshi viewport test (temporary)', initialize
its terminal, and pass --workspace UUID. No working terminals are targeted.
--mirror permits running identical checks against the pre-fix source.
"""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--mirror', type=Path, default=ROOT/'claude-hooks/moshi-cmux-mirror.py')
    args = parser.parse_args()
    cli = shutil.which('cmux')

    def rpc(method, params):
        result = subprocess.run([cli, 'rpc', method, json.dumps(params)],
                                capture_output=True, text=True, timeout=6)
        if result.returncode:
            return {'_error': result.stderr.strip().splitlines()[-1]}
        return json.loads(result.stdout)

    tree = rpc('system.tree', {})
    workspaces = [ws for w in tree['windows'] for ws in w['workspaces'] if ws['id'] == args.workspace]
    assert len(workspaces) == 1 and workspaces[0]['title'] == 'Moshi viewport test (temporary)'
    surfaces = [s for p in workspaces[0]['panes'] for s in p['surfaces']]
    assert len(surfaces) == 1 and surfaces[0]['type'] == 'terminal'
    sid = surfaces[0]['id']

    def geometry():
        reply = rpc('terminal.replay', {'surface_id': sid})
        return reply.get('columns'), reply.get('rows')

    desktop = geometry()
    assert all(desktop), 'initialize the disposable terminal before this smoke'
    assert desktop[0] > 52 and desktop[1] > 42, desktop
    rpc('surface.send_text', {'surface_id': sid, 'text': "i=0; while [ $i -lt 200 ]; do echo NATIVE_HISTORY_$i; i=$((i+1)); done\n"})
    deadline = time.monotonic()+8
    while 'NATIVE_HISTORY_199' not in rpc('surface.read_text', {'surface_id': sid, 'lines': 100}).get('text', ''):
        assert time.monotonic() < deadline, 'disposable shell must finish producing scrollback'
        time.sleep(.1)
    with tempfile.TemporaryDirectory(prefix='moshi-viewport-') as tmp:
        path = Path(tmp)
        (path/'state.json').write_text(json.dumps({'surface_id': sid, 'grouped': True}))
        visible = threading.Event()
        visible.set()
        stop_sampler = threading.Event()

        def sampler():
            while not stop_sampler.is_set():
                sample = {'updated': time.time(), 'panes': ['%test'] if visible.is_set() else [], 'activity': time.time()}
                staged = path/'visibility.tmp'
                staged.write_text(json.dumps(sample))
                staged.replace(path/'visibility.json')
                stop_sampler.wait(.1)

        thread = threading.Thread(target=sampler)
        thread.start()
        # A single injected clear error exercises retry against the actual
        # cmux server. All successful RPCs and all geometry come from cmux.
        wrapper = path/'cmux'
        wrapper.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['VIEWPORT_SMOKE_ROOT'])
method=sys.argv[2]; params=json.loads(sys.argv[3])
assert params['surface_id']==os.environ['VIEWPORT_SMOKE_SURFACE']
assert method in {'terminal.replay','terminal.viewport','surface.read_text'}
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps([method,params])+'\\n')
if method=='terminal.viewport' and (root/'fail-clear').exists():
 (root/'fail-clear').unlink(); print('injected transient clear failure',file=sys.stderr);sys.exit(1)
sys.exit(subprocess.call([os.environ['VIEWPORT_SMOKE_CLI'],*sys.argv[1:]]))
''')
        wrapper.chmod(0o755)
        env = dict(os.environ, CMUX_BIN=str(wrapper), TMUX='native-test,1,0', TMUX_PANE='%test',
                   MOSHI_LOG_PATH=str(path/'mirror.log'), VIEWPORT_SMOKE_ROOT=str(path),
                   VIEWPORT_SMOKE_SURFACE=sid, VIEWPORT_SMOKE_CLI=cli)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 29, 37, 0, 0))
        proc = subprocess.Popen([sys.executable, str(args.mirror), str(path/'state.json'), str(path/'queue')],
                                stdin=slave, stdout=slave, stderr=slave, env=env)
        output = bytearray()
        other_client = 'moshi-native-other-client'

        def drain():
            if select.select([master], [], [], .05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise

        def wait(predicate, label, timeout=8):
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                drain()
                if predicate():
                    return
            raise AssertionError(label + ': geometry=' + repr(geometry()))

        try:
            wait(lambda: geometry() == (37, 29), 'phone attach')
            os.write(master, b'\x1b[<64;5;5M')
            wait(lambda: b'scrollback -3' in output, 'history screen')
            # Exceeds cmux's observed native expiry; painting history must keep
            # refreshing the same report without resizing back to the desktop.
            start = time.monotonic()
            while time.monotonic()-start < 8:
                drain()
                assert geometry() == (37, 29), 'phone lease expired while reading history'
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 42, 52, 0, 0))
            proc.send_signal(signal.SIGWINCH)
            output.clear()
            wait(lambda: geometry() == (52, 42) and b'\x1b[42;1H' in output, 'rotation during history')
            (path/'fail-clear').touch()
            visible.clear()
            wait(lambda: geometry() == desktop, 'desktop restored after failed clear')
            assert not (path/'fail-clear').exists(), 'failure injection must be consumed'
            visible.set()
            wait(lambda: geometry() == (52, 42), 'phone reconnect')
            proc.terminate()
            proc.wait(timeout=8)
            wait(lambda: geometry() == desktop, 'desktop restored on normal exit')
            # Restart and SIGKILL: native lease expiry must restore without any
            # Python finally block or watcher assistance.
            proc = subprocess.Popen([sys.executable, str(args.mirror), str(path/'state.json'), str(path/'queue')],
                                    stdin=slave, stdout=slave, stderr=slave, env=env)
            wait(lambda: geometry() == (52, 42), 'reattach before crash')
            proc.kill()
            proc.wait(timeout=8)
            wait(lambda: geometry() == desktop, 'native expiry after SIGKILL', timeout=12)
            rpc('terminal.viewport', {'surface_id': sid, 'client_id': other_client,
                                     'viewport_columns': 45, 'viewport_rows': 36})
            wait(lambda: geometry() == (45, 36), 'independent client viewport')
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 29, 37, 0, 0))
            proc = subprocess.Popen([sys.executable, str(args.mirror), str(path/'state.json'), str(path/'queue')],
                                    stdin=slave, stdout=slave, stderr=slave, env=env)
            wait(lambda: geometry() == (37, 29), 'second client coexists with phone')
            proc.terminate()
            proc.wait(timeout=8)
            wait(lambda: geometry() == (45, 36), 'cleanup preserves independent client')
            rpc('terminal.viewport', {'surface_id': sid, 'client_id': other_client, 'clear': True})
            wait(lambda: geometry() == desktop, 'native desktop after both clients leave')
            print(json.dumps({'result': 'PASS', 'desktop': desktop, 'history_lease': True,
                              'history_rotation': True, 'clear_failure_retry': True,
                              'reconnect': True, 'normal_exit': True, 'crash_expiry': True,
                              'independent_client_preserved': True}))
        except Exception:
            print('Disposable terminal output tail: '+repr(output[-600:]), file=sys.stderr)
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            stop_sampler.set()
            thread.join(timeout=2)
            # Exact client IDs come only from this smoke's recorded requests.
            calls = path/'calls.jsonl'
            clients = {p['client_id'] for m, p in map(json.loads, calls.read_text().splitlines())
                       if 'client_id' in p} if calls.exists() else set()
            for client in clients:
                rpc('terminal.viewport', {'surface_id': sid, 'client_id': client, 'clear': True})
            rpc('terminal.viewport', {'surface_id': sid, 'client_id': other_client, 'clear': True})
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    main()
