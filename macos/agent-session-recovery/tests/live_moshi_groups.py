#!/usr/bin/env python3
"""Opt-in live smoke test against a disposable cmux workspace with two shells.

Usage: python3 live_moshi_groups.py --workspace <UUID>
The caller creates/closes the workspace. Never point this at working terminals.
"""
import argparse
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workspace',required=True)
args=parser.parse_args()
real='/opt/homebrew/bin/cmux'
data=json.loads(subprocess.check_output([real,'rpc','system.tree','{}']))
windows=[]
for window in data['windows']:
    workspace=[s for s in window['workspaces'] if s['id']==args.workspace]
    if workspace: windows.append(dict(window,workspaces=workspace))
assert len(windows)==1
workspace=windows[0]['workspaces'][0]
assert workspace['title']=='Moshi integration test (temporary)', 'requires disposable test workspace title'
surfaces=workspace['panes'][0]['surfaces']
assert len(surfaces)==2 and all(s['type']=='terminal' for s in surfaces)
ids=[s['id'] for s in surfaces]
socket='moshi-live-'+uuid.uuid4().hex
client=None
master=None
with tempfile.TemporaryDirectory(prefix='moshi-live-') as tmp:
    path=Path(tmp)
    wrapper=path/'cmux'
    wrapper.write_text('''#!/usr/bin/env python3
import json,subprocess,sys
method=sys.argv[2]
if method=='system.tree':
 print(TREE)
else:
 params=json.loads(sys.argv[3])
 assert params['surface_id'] in IDS
 assert method in {'terminal.replay','terminal.viewport','surface.send_key','surface.send_text'}
 subprocess.run([REAL,*sys.argv[1:]],check=True)
'''.replace('TREE',repr(json.dumps({'windows':windows}))).replace('IDS',repr(ids)).replace('REAL',repr(real)))
    wrapper.chmod(0o755)
    env=os.environ.copy()
    env.update(TMUX_SOCKET=socket,CMUX_BIN=str(wrapper),MOSHI_GROUP_STATE_DIR=str(path/'state'),MOSHI_LOG_PATH=str(path/'mirror.log'),TERM='xterm-256color')
    def tmux(*a): return subprocess.check_output(['tmux','-L',socket,*a],text=True,env=env).strip()
    try:
        tmux('-f','/dev/null','new-session','-d','-s','test-anchor','sleep 120')
        result=subprocess.run([sys.executable,str(ROOT/'claude-hooks/moshi-cmux-groups.py')],env=env,text=True,capture_output=True,check=True)
        states=[json.loads(p.read_text()) for p in (path/'state').glob('*.json')]
        states.sort(key=lambda x:ids.index(x['surface_id']))
        a,b=states
        tmux('select-window','-t',a['tmux_window'])
        master,slave=pty.openpty()
        client=subprocess.Popen(['tmux','-L',socket,'attach-session','-t',a['tmux_session']],stdin=slave,stdout=slave,stderr=slave,env=env)
        os.close(slave)
        def wait(pred):
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]: os.read(master,65536)
                if pred(): return
            raise AssertionError('live terminal check timed out')
        wait(lambda:tmux('display-message','-p','-t',a['tmux_pane'],'#{session_attached}')=='1')
        # Prefix+n selects B; type a command through the mirror, then verify real output.
        os.write(master,b'\x02n')
        wait(lambda:tmux('display-message','-p','-t',a['tmux_session'],'#{pane_id}')==b['tmux_pane'])
        time.sleep(2)
        os.write(master,b"printf 'MOSHI_REAL_TAB_B_OK\\n'\r")
        wait(lambda:'MOSHI_REAL_TAB_B_OK' in tmux('capture-pane','-p','-t',b['tmux_pane']))
        os.write(master,b'\x02p')
        wait(lambda:tmux('display-message','-p','-t',a['tmux_session'],'#{pane_id}')==a['tmux_pane'])
        time.sleep(2)
        os.write(master,b"printf 'MOSHI_REAL_TAB_A_OK\\n'\r")
        wait(lambda:'MOSHI_REAL_TAB_A_OK' in tmux('capture-pane','-p','-t',a['tmux_pane']))
        # Actual Moshi context resolves the grouped tmux window/pane.
        context_env=env.copy()
        context_env['TMUX']=tmux('display-message','-p','-t',a['tmux_pane'],'#{socket_path},#{pid},#{session_id}')
        context_env['TMUX_PANE']=a['tmux_pane']
        context=json.loads(subprocess.check_output(['/opt/homebrew/bin/moshi','context'],env=context_env,text=True))
        print(json.dumps({'result':'PASS','tabs':2,'prefix_next_previous':True,'input_and_replay':True,'moshi_context':context}))
    finally:
        subprocess.run(['tmux','-L',socket,'kill-server'],capture_output=True)
        if client: client.kill();client.wait()
        if master is not None: os.close(master)
