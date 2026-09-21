#!/usr/bin/env python3
"""Real isolated tmux tests. No user terminals, hooks, or pairing are used."""
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

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'claude-hooks'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.socket = 'moshi-test-'+uuid.uuid4().hex
        self.env = os.environ.copy()
        os.environ['TMUX_SOCKET'] = self.socket
        os.environ['MOSHI_GROUP_STATE_DIR'] = str(self.path/'state')
        self.fake = self.path/'cmux'
        self.fake.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['FAKE_CMUX_ROOT'])
method=sys.argv[2]
if method=='system.tree':
 print((root/'tree.json').read_text())
else:
 with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(sys.argv[2:])+'\\n')
 print(json.dumps({'render_grid':{'render_revision':1,'row_spans':[{'row':0,'column':0,'text':'MIRROR-TEST'}]}}))
''')
        self.fake.chmod(0o755)
        os.environ['CMUX_BIN'] = str(self.fake)
        os.environ['FAKE_CMUX_ROOT'] = str(self.path)
        os.environ['TERM'] = 'xterm-256color'
        os.environ['MOSHI_LOG_PATH'] = str(self.path/'mirror.log')
        fake_moshi = self.path/'moshi'
        fake_moshi.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\ne=json.load(sys.stdin)\nPath(os.environ['FAKE_CMUX_ROOT'],'hook.json').write_text(json.dumps({'payload':e,'pane':os.environ.get('TMUX_PANE')}))\nprint(json.dumps({'decision':'allow'}))\n")
        fake_moshi.chmod(0o755)
        os.environ['MOSHI_BIN'] = str(fake_moshi)
        self.mod = load('moshi-cmux-groups')
        self.mod.STATE.mkdir()
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'unrelated', 'sleep 120')
        self.data = {'windows':[{'id':'W1','workspaces':[{'id':'WS1','title':'Project','panes':[
            {'id':'P1','surfaces':[self.surface('A',0),self.surface('B',1),self.surface('browser',2,'browser')]},
            {'id':'P2','surfaces':[self.surface('C',0)]}]}]},
            {'id':'W2','workspaces':[{'id':'WS2','title':'Project','panes':[{'id':'P3','surfaces':[self.surface('D',0)]}]}]}]}
        self.write_tree()
        self.clients=[]

    @staticmethod
    def surface(sid, index, kind='terminal'):
        return dict(id=sid, title='Tab '+sid, index_in_pane=index, type=kind)

    def write_tree(self):
        (self.path/'tree.json').write_text(json.dumps(self.data))

    def tmux(self,*args):
        return subprocess.check_output(['tmux','-L',self.socket,*args],text=True).strip()

    def tearDown(self):
        subprocess.run(['tmux','-L',self.socket,'kill-server'],capture_output=True)
        for proc, fd in self.clients:
            proc.kill()
            proc.wait(timeout=5)
            os.close(fd)
        subprocess.run(['tmux','-L',self.socket,'kill-server'],capture_output=True)
        os.environ.clear();os.environ.update(self.env)
        self.tmp.cleanup()

    def test_group_order_rename_close_move_and_idempotence(self):
        first=self.mod.sync(self.data)
        self.assertEqual(len(first),4)
        self.assertEqual(first['A'][2]['session'],first['B'][2]['session'])
        self.assertNotEqual(first['A'][2]['session'],first['C'][2]['session'])
        self.assertNotEqual(first['C'][2]['session'],first['D'][2]['session'])
        original={s:r[2]['pane'] for s,r in first.items()}
        again=self.mod.sync(self.data)
        self.assertEqual(original,{s:r[2]['pane'] for s,r in again.items()})
        surfaces=self.data['windows'][0]['workspaces'][0]['panes'][0]['surfaces']
        surfaces[0]['index_in_pane']=1;surfaces[1]['index_in_pane']=0
        surfaces[0]['title']='Renamed A'
        changed=self.mod.sync(self.data)
        self.assertEqual(self.tmux('list-windows','-t',changed['A'][2]['session'],'-F','#{window_name}'),'Tab B\nRenamed A')
        self.assertEqual(original['A'],changed['A'][2]['pane'])
        # Move a horizontal tab to a different strip and preserve its pane ID.
        moved=surfaces.pop(0);moved['index_in_pane']=1
        self.data['windows'][0]['workspaces'][0]['panes'][1]['surfaces'].append(moved)
        changed=self.mod.sync(self.data)
        self.assertEqual(original['A'],changed['A'][2]['pane'])
        self.assertEqual(changed['A'][2]['session'],changed['C'][2]['session'])
        self.data['windows'][1]['workspaces']=[]
        changed=self.mod.sync(self.data)
        self.assertNotIn('D',changed)
        self.assertEqual(self.tmux('display-message','-p','-t','unrelated:','#{session_name}'),'unrelated')

    def test_hook_runs_in_correct_grouped_pane(self):
        bindings=self.mod.sync(self.data)
        env=os.environ.copy();env['CMUX_SURFACE_ID']='B'
        event={'hook_event_name':'PreToolUse','session_id':'test-session','cwd':str(self.path)}
        result=subprocess.run([sys.executable,str(ROOT/'claude-hooks/moshi-cmux-groups.py'),'--hook'],
                              input=json.dumps(event),text=True,capture_output=True,env=env,timeout=25)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'decision':'allow'})
        observed=json.loads((self.path/'hook.json').read_text())
        self.assertEqual(observed['pane'],bindings['B'][2]['pane'])
        self.assertEqual(observed['payload'],event)

    def test_concurrent_reconciliation_does_not_duplicate_windows(self):
        processes=[subprocess.Popen([sys.executable,str(ROOT/'claude-hooks/moshi-cmux-groups.py')],
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
        for proc in processes:
            out,err=proc.communicate(timeout=30)
            self.assertEqual(proc.returncode,0,err)
        rows=[r for r in self.mod.inventory() if r['group']]
        self.assertEqual(len(rows),4)
        self.assertEqual(len({r['surface'] for r in rows}),4)
        first={r['surface']:r['pane'] for r in rows}
        self.data['windows'][0]['workspaces'][0]['title']='Renamed workspace'
        self.mod.sync(self.data)
        self.assertEqual(first,{r['surface']:r['pane'] for r in self.mod.inventory() if r['group']})

    def test_shared_visibility_sample_is_pane_specific(self):
        mirror=load('moshi-cmux-mirror')
        os.environ['TMUX']='test,1,0';os.environ['TMUX_PANE']='%2'
        sample=self.path/'visibility.json'
        sample.write_text(json.dumps({'updated':time.time(),'panes':['%1']}))
        self.assertFalse(mirror.session_attached(sample))
        sample.write_text(json.dumps({'updated':time.time(),'panes':['%2']}))
        self.assertTrue(mirror.session_attached(sample))

    def test_tree_failure_preserves_existing_views(self):
        self.mod.sync(self.data)
        before=self.tmux('list-panes','-a','-F','#{pane_id}')
        (self.path/'tree.json').write_text('{}')
        result=subprocess.run([sys.executable,str(ROOT/'claude-hooks/moshi-cmux-groups.py')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(before,self.tmux('list-panes','-a','-F','#{pane_id}'))

    def test_prefix_swipes_input_and_visible_only_viewport(self):
        bindings=self.mod.sync(self.data)
        a=bindings['A'][2];b=bindings['B'][2]
        self.tmux('select-window','-t',a['window'])
        master,slave=pty.openpty()
        proc=subprocess.Popen(['tmux','-L',self.socket,'attach-session','-t',a['session']],stdin=slave,stdout=slave,stderr=slave,env=os.environ.copy())
        os.close(slave);self.clients.append((proc,master))
        def wait_for(predicate):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]: os.read(master,65536)
                if predicate(): return
            self.fail('timed out waiting for terminal behavior')
        wait_for(lambda:self.tmux('display-message','-p','-t',a['pane'],'#{session_attached}')=='1')
        os.write(master,b'\x02n') # exactly Moshi's documented swipe bytes
        wait_for(lambda:self.tmux('display-message','-p','-t',a['session'],'#{pane_id}')==b['pane'])
        os.write(master,b'x')
        calls=self.path/'calls.jsonl'
        wait_for(lambda:calls.exists() and 'surface.send_text' in calls.read_text())
        records=[json.loads(line) for line in calls.read_text().splitlines()]
        sent=[json.loads(r[1]) for r in records if r[0]=='surface.send_text']
        self.assertEqual(sent[-1],{'surface_id':'B','text':'x'})
        self.assertFalse(any(json.loads(r[1])['surface_id'] in {'C','D'} for r in records))
        os.write(master,b'\x02p')
        wait_for(lambda:self.tmux('display-message','-p','-t',a['session'],'#{pane_id}')==a['pane'])
        wait_for(lambda:'MIRROR-TEST' in self.tmux('capture-pane','-p','-t',a['pane']))


if __name__=='__main__':
    unittest.main()
