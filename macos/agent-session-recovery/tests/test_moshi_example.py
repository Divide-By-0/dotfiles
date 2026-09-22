#!/usr/bin/env python3
"""Minimal demo tests; --live-workspace UUID uses two disposable real shells."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'examples/moshi-cmux-tabs.py'
spec = importlib.util.spec_from_file_location('demo', SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
LIVE = None
if '--live-workspace' in sys.argv:
    i = sys.argv.index('--live-workspace')
    LIVE = sys.argv[i + 1]
    del sys.argv[i:i + 2]


class ExampleTests(unittest.TestCase):
    def test_selects_only_ordered_sibling_terminals(self):
        surfaces = [dict(id='B', type='terminal', index_in_pane=2),
                    dict(id='web', type='browser', index_in_pane=1),
                    dict(id='A', type='terminal', index_in_pane=0)]
        tree = {'windows': [{'workspaces': [{'panes': [
            {'surfaces': surfaces}, {'surfaces': [dict(id='other', type='terminal', index_in_pane=0)]}]}]}]}
        self.assertEqual([s['id'] for s in demo.siblings(tree, 'B')], ['A', 'B'])
        with self.assertRaises(ValueError):
            demo.siblings(tree, 'missing')

    def test_real_tmux_switching_input_and_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            if LIVE:
                tree = demo.rpc('system.tree')
                workspaces = [w for win in tree['windows'] for w in win['workspaces'] if w['id'] == LIVE]
                self.assertEqual(len(workspaces), 1)
                self.assertEqual(workspaces[0]['title'], 'Moshi minimal example test (temporary)')
                surfaces = workspaces[0]['panes'][0]['surfaces']
                self.assertEqual(len(surfaces), 2)
                self.assertTrue(all(s['type'] == 'terminal' for s in surfaces))
                ids = [s['id'] for s in sorted(surfaces, key=lambda s: s['index_in_pane'])]
                cmux = '/opt/homebrew/bin/cmux'
            else:
                ids = ['A', 'B']
                tree = {'windows': [{'workspaces': [{'panes': [{'surfaces': [
                    dict(id=s, type='terminal', index_in_pane=i) for i,s in enumerate(ids)]}]}]}]}
                fake = path / 'cmux'
                fake.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
p=Path(__file__).parent
method=sys.argv[2];args=json.loads(sys.argv[3])
if method=='system.tree': print((p/'tree.json').read_text())
else:
 with (p/'calls').open('a') as f: f.write(json.dumps([method,args])+'\\n')
 print(json.dumps({'render_grid':{'row_spans':[{'row':0,'column':0,'text':'SCREEN-'+args['surface_id']}]}}))
''')
                fake.chmod(0o755)
                (path/'tree.json').write_text(json.dumps(tree))
                cmux = str(fake)
            socket = 'moshi-example-test-' + uuid.uuid4().hex
            client = master = None
            with patch.dict(os.environ, TMUX_SOCKET=socket, CMUX_BIN=cmux, TERM='xterm-256color'):
                try:
                    demo.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'anchor', 'sleep 120')
                    demo.tmux('set-option', '-g', 'remain-on-exit', 'on')
                    session = demo.create(ids[0])
                    panes = demo.tmux('list-panes', '-s', '-t', session, '-F', '#{pane_id}').splitlines()
                    self.assertEqual(len(panes), 2)
                    master, slave = pty.openpty()
                    client = subprocess.Popen(['tmux', '-L', socket, 'attach-session', '-t', session],
                                              stdin=slave, stdout=slave, stderr=slave)
                    os.close(slave)
                    def wait(predicate):
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            if select.select([master], [], [], .1)[0]:
                                os.read(master, 65536)
                            if predicate(): return
                        print(demo.tmux('capture-pane','-p','-t',session))
                        self.fail('terminal interaction timed out')
                    wait(lambda: demo.tmux('display-message','-p','-t',session,'#{session_attached}') == '1')
                    for index, gesture in [(1, b'\x02n'), (0, b'\x02p')]:
                        os.write(master, gesture)
                        wait(lambda: demo.tmux('display-message','-p','-t',session,'#{pane_id}') == panes[index])
                        wait(lambda: bool(demo.tmux('capture-pane','-p','-t',panes[index])))
                        if LIVE:
                            # Expected output is not present in the echoed command.
                            marker = uuid.uuid4().hex
                            os.write(master, f"printf 'DEMO_%s_OK\\n' {marker}\r".encode())
                            wait(lambda: f'DEMO_{marker}_OK' in demo.tmux('capture-pane','-p','-t',panes[index]))
                        else:
                            os.write(master, b'hello\r')
                            wait(lambda: (path/'calls').exists() and any(
                                method == 'surface.send_key' and args == {'surface_id':ids[index], 'key':'enter'}
                                for method,args in map(json.loads,(path/'calls').read_text().splitlines())))
                            self.assertIn('SCREEN-'+ids[index], demo.tmux('capture-pane','-p','-t',panes[index]))
                    if LIVE:
                        env = dict(os.environ, TMUX=demo.tmux('display-message','-p','-t',panes[0],
                                   '#{socket_path},#{pid},#{session_id}'), TMUX_PANE=panes[0])
                        context = json.loads(subprocess.check_output(['moshi','context'],env=env,text=True))
                        print('Live Moshi context:', json.dumps(context))
                    self.assertEqual(demo.tmux('list-panes','-s','-t',session,'-F','#{pane_dead}'), '0\n0')
                    self.assertEqual(demo.tmux('display-message','-p','-t','anchor:','#{session_name}'), 'anchor')
                finally:
                    subprocess.run(['tmux','-L',socket,'kill-server'],capture_output=True)
                    if client:
                        client.kill(); client.wait(timeout=5)
                    if master is not None: os.close(master)


if __name__ == '__main__':
    unittest.main()
