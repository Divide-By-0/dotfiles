#!/usr/bin/env python3
"""Real isolated tmux tests. No user terminals, hooks, or pairing are used."""
import importlib.util
import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
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
elif method=='surface.read_text':
 with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(sys.argv[2:])+'\\n')
 print(json.dumps({'text':'\\n'.join('HISTORY %d' % i for i in range(200))+'\\n'}))
elif method=='surface.create':
 params=json.loads(sys.argv[3])
 with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(sys.argv[2:])+'\\n')
 tree=json.loads((root/'tree.json').read_text())
 sid='NEW%d'%sum(1 for _ in (root/'calls.jsonl').open())
 for w in tree['windows']:
  for ws in w['workspaces']:
   for pane in ws['panes']:
    if pane['id']==params['pane_id']:
     pane['surfaces'].append(dict(id=sid,title='Terminal',index_in_pane=len(pane['surfaces']),type='terminal'))
 (root/'tree.json').write_text(json.dumps(tree))
 print(json.dumps({'surface_id':sid,'pane_id':params['pane_id']}))
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

    def test_tmux_new_window_in_group_creates_cmux_tab(self):
        """A tab opened on the phone (plain tmux new-window) becomes a real cmux tab."""
        first=self.mod.sync(self.data)
        session=first['A'][2]['session']
        wid,pid=self.tmux('new-window','-d','-P','-F','#{window_id} #{pane_id}','-t',session+':','/bin/sh').split()
        adopted=self.mod.sync(self.data)
        calls=[json.loads(l) for l in (self.path/'calls.jsonl').read_text().splitlines()]
        creates=[json.loads(c[1]) for c in calls if c[0]=='surface.create']
        self.assertEqual(len(creates),1)
        self.assertEqual({k:creates[0][k] for k in ('type','pane_id','workspace_id','window_id','focus')},
                         {'type':'terminal','pane_id':'P1','workspace_id':'WS1','window_id':'W1','focus':False})
        new_sid=next(s for s in adopted if s.startswith('NEW'))
        # Same window and pane the phone is looking at, now bound and mirroring the new surface.
        self.assertEqual(adopted[new_sid][2]['window'],wid)
        self.assertEqual(adopted[new_sid][2]['pane'],pid)
        self.assertEqual(self.tmux('show-options','-wqv','-t',wid,self.mod.SURFACE),new_sid)
        self.assertIn('moshi-cmux-mirror.py',self.tmux('display-message','-p','-t',pid,'#{pane_start_command}'))
        # cmux's tree now contains the tab; later syncs neither create again nor duplicate windows.
        self.data=json.loads((self.path/'tree.json').read_text())
        self.mod.sync(self.data)
        creates=[l for l in (self.path/'calls.jsonl').read_text().splitlines() if 'surface.create' in l]
        self.assertEqual(len(creates),1)
        self.assertEqual(len(self.tmux('list-windows','-t',session+':','-F','#{window_id}').split()),3)

    def test_busy_phone_window_is_never_adopted_or_killed(self):
        """2026-09-26: a Claude started in a phone-made tab was killed by adoption.

        Only an idle shell may be replaced by a mirror; a window running anything
        else keeps its process, and no cmux tab is created for it.
        """
        first=self.mod.sync(self.data)
        session=first['A'][2]['session']
        wid,pid=self.tmux('new-window','-d','-P','-F','#{window_id} #{pane_id}','-t',session+':',
                          '/bin/sh -c "sleep 120"').split()
        time.sleep(0.3)
        busy_pid=self.tmux('display-message','-p','-t',pid,'#{pane_pid}')
        self.mod.sync(self.data)
        calls=(self.path/'calls.jsonl').read_text() if (self.path/'calls.jsonl').exists() else ''
        self.assertNotIn('surface.create',calls)
        self.assertEqual(self.tmux('display-message','-p','-t',pid,'#{pane_pid} #{pane_dead}'),busy_pid+' 0')
        self.assertEqual(self.tmux('show-options','-wqv','-t',wid,self.mod.SURFACE),'')

    def test_exited_mirror_keeps_its_window_in_every_position(self):
        """remain-on-exit must hold for every window, not only a session's first.

        2026-09-27: restarting mirrors closed 42 of 51 windows (only index 0 had
        the option), so they came back with new IDs under the phone.
        """
        first=self.mod.sync(self.data)
        wid,pid=first['B'][2]['window'],first['B'][2]['pane']
        self.assertNotEqual(self.tmux('display-message','-p','-t',wid,'#{window_index}'),'0')
        self.tmux('respawn-pane','-k','-t',pid,'true')
        time.sleep(0.5)
        self.assertEqual(self.tmux('display-message','-p','-t',pid,'#{window_id} #{pane_dead}'),wid+' 1')
        again=self.mod.sync(self.data)
        self.assertEqual((again['B'][2]['window'],again['B'][2]['pane']),(wid,pid))

    def test_one_shot_sync_skips_instead_of_queueing_on_the_lock(self):
        """Hook-launched syncs must not pile up behind a running sync."""
        import fcntl
        with (self.mod.STATE/'.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            start=time.monotonic()
            result=subprocess.run([sys.executable,str(ROOT/'claude-hooks/moshi-cmux-groups.py')],
                                  capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertLess(time.monotonic()-start,5)
        self.assertEqual([r for r in self.mod.inventory() if r['group']],[])

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

    def test_unowned_restored_name_collision_is_preserved(self):
        name=self.mod.groups(self.data)[0]['name']
        old=self.tmux('new-session','-d','-P','-F','#{pane_id}','-s',name,'sleep 120')
        bindings=self.mod.sync(self.data)
        self.assertNotEqual(bindings['A'][2]['pane'],old)
        self.assertEqual(self.tmux('display-message','-p','-t',old,'#{session_name}'),name)
        panes={s:b[2]['pane'] for s,b in bindings.items()}
        self.assertEqual(panes,{s:b[2]['pane'] for s,b in self.mod.sync(self.data).items()})

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
        termios.tcsetwinsize(slave, (30, 37))
        proc=subprocess.Popen(['tmux','-L',self.socket,'attach-session','-t',a['session']],stdin=slave,stdout=slave,stderr=slave,env=os.environ.copy())
        os.close(slave);self.clients.append((proc,master))
        def wait_for(predicate):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try:
                        os.read(master,65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise  # Linux reports EIO when the detached client closes its PTY.
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
        # These are the real cmux resize inputs; columns/rows are silently ignored.
        def reports(sid):
            return [json.loads(r[1]) for r in map(json.loads, calls.read_text().splitlines())
                    if r[0]=='terminal.replay' and json.loads(r[1])['surface_id']==sid]
        wait_for(lambda: reports('B'))
        report=reports('B')[-1]
        self.assertEqual(report['viewport_columns'],37)
        self.assertEqual(report['viewport_rows'],29)
        self.assertTrue(report['client_id'].startswith('moshi-tmux-'))
        self.assertNotIn('columns',report)
        termios.tcsetwinsize(master, (43, 52))
        proc.send_signal(signal.SIGWINCH)  # openpty/Popen has no controlling tty
        wait_for(lambda: reports('B')[-1].get('viewport_columns')==52)
        self.assertEqual(reports('B')[-1]['viewport_rows'],42)
        def cleared(sid):
            return any(r[0]=='terminal.viewport' and json.loads(r[1]).get('surface_id')==sid
                       and json.loads(r[1]).get('clear') is True
                       for r in map(json.loads,calls.read_text().splitlines()))
        os.write(master,b'\x02p')
        wait_for(lambda:self.tmux('display-message','-p','-t',a['session'],'#{pane_id}')==a['pane'])
        wait_for(lambda:'MIRROR-TEST' in self.tmux('capture-pane','-p','-t',a['pane']))
        # Swiping away holds the phone width: releasing it on every swipe made
        # Claude reprint its conversation into the desktop scrollback each time.
        time.sleep(2)
        self.assertFalse(cleared('B'))
        os.write(master,b'\x02d')  # phone gone: release every held viewport
        wait_for(lambda:cleared('A') and cleared('B'))

    def test_phone_wheel_scrolls_mirror_through_cmux_scrollback(self):
        """A vertical swipe (SGR wheel via tmux mouse mode) shows cmux scrollback."""
        bindings=self.mod.sync(self.data)
        a=bindings['A'][2]
        self.tmux('set','-g','mouse','on')
        # The binding shipped in config/tmux-session-recovery.conf.
        conf=(ROOT/'config/tmux-session-recovery.conf').read_text()
        wheel=next(l for l in conf.splitlines() if l.startswith('bind -T root WheelUpPane'))
        subprocess.run(['tmux','-L',self.socket,'source-file','-'],input=wheel+'\n',text=True,check=True)
        self.tmux('select-window','-t',a['window'])
        master,slave=pty.openpty()
        termios.tcsetwinsize(slave,(30,37))
        proc=subprocess.Popen(['tmux','-L',self.socket,'attach-session','-t',a['session']],stdin=slave,stdout=slave,stderr=slave,env=os.environ.copy())
        os.close(slave);self.clients.append((proc,master))
        def wait_for(predicate,what):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try: os.read(master,65536)
                    except OSError as exc:
                        if exc.errno!=errno.EIO: raise
                if predicate(): return
            self.fail('timed out waiting for '+what)
        wait_for(lambda:'MIRROR-TEST' in self.tmux('capture-pane','-p','-t',a['pane']),'live mirror')
        wait_for(lambda:self.tmux('display-message','-p','-t',a['pane'],'#{mouse_any_flag}')=='1','mouse reporting')
        os.write(master,b'\x1b[<64;5;5M')  # what the phone's terminal sends for wheel-up
        wait_for(lambda:'scrollback -3' in self.tmux('capture-pane','-p','-t',a['pane']),'scrollback view')
        screen=self.tmux('capture-pane','-p','-t',a['pane'])
        self.assertIn('HISTORY 196',screen)  # 3 lines above the bottom of 200
        self.assertEqual(self.tmux('display-message','-p','-t',a['pane'],'#{pane_in_mode}'),'0')
        os.write(master,b'\x1b[<65;5;5M')
        wait_for(lambda:'MIRROR-TEST' in self.tmux('capture-pane','-p','-t',a['pane']),'return to live')
        sent=[l for l in (self.path/'calls.jsonl').read_text().splitlines() if 'send_text' in l or 'send_key' in l]
        self.assertEqual(sent,[])  # nothing typed into the cmux terminal

    def test_stale_desktop_grid_waits_for_reflow(self):
        mirror=load('moshi-cmux-mirror')
        self.assertFalse(mirror.grid_fits({'columns':156,'rows':51},37,29))
        self.assertTrue(mirror.grid_fits({'columns':37,'rows':29},37,29))
        self.assertTrue(mirror.grid_fits({'columns':30,'rows':20},37,29))


if __name__=='__main__':
    unittest.main()
