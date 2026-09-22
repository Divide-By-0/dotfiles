#!/usr/bin/env python3
"""Expose each cmux tab strip as a tmux session, preserving surface identity.

Only sessions/windows carrying our ownership options are mutated. system.tree
is authoritative; an RPC error never means that the user's tabs were closed.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('MOSHI_GROUP_STATE_DIR', str(Path.home() / '.claude/hooks/moshi-cmux-groups')))
OWNER = '@moshi_cmux_group'
SURFACE = '@moshi_cmux_surface'


def command(args, *, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=10)


def tmux(*args, check=True):
    cmd = [os.environ.get('TMUX_BIN') or shutil.which('tmux') or '/opt/homebrew/bin/tmux']
    if os.environ.get('TMUX_SOCKET'):
        cmd += ['-L', os.environ['TMUX_SOCKET']]
    return command(cmd + list(args), check=check)


def tree():
    exe = os.environ.get('CMUX_BIN') or shutil.which('cmux') or '/opt/homebrew/bin/cmux'
    data = json.loads(command([exe, 'rpc', 'system.tree', '{}']).stdout)
    if not isinstance(data.get('windows'), list):
        raise ValueError('cmux returned no authoritative window list')
    return data


def groups(data):
    result = []
    for window in data['windows']:
        for workspace in window['workspaces']:
            for pane in workspace['panes']:
                surfaces = sorted((s for s in pane['surfaces'] if s['type'] == 'terminal'),
                                  key=lambda s: s['index_in_pane'])
                if not surfaces:
                    continue
                identity = '/'.join([window['id'], workspace['id'], pane['id']])
                digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
                slug = re.sub(r'[^a-z0-9]+', '-', workspace['title'].lower()).strip('-')[:28] or 'workspace'
                result.append(dict(key=identity, name=f'cmux-{slug}-{digest}', window_id=window['id'],
                                   workspace_id=workspace['id'], pane_id=pane['id'], surfaces=surfaces))
    return result


def atomic(path, data):
    if load(path) == data:
        return
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, path)


def state_path(sid):
    return STATE / (hashlib.sha256(sid.encode()).hexdigest() + '.json')


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def inventory():
    result = tmux('list-windows', '-a', '-F',
                  '#{session_id}\t#{session_name}\t#{'+OWNER+'}\t#{window_id}\t#{window_index}\t#{'+SURFACE+'}\t#{pane_id}\t#{pane_dead}\t#{window_name}', check=False)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) == 9:
            rows.append(dict(zip(['session', 'name', 'group', 'window', 'index', 'surface', 'pane', 'dead', 'title'], fields)))
    return rows


def sync(data):
    desired = groups(data)
    rows = inventory()
    by_surface = {}
    sessions = {}
    for row in rows:
        if row['group']:
            sessions[row['group']] = row['session']
            if row['surface']:
                if row['surface'] in by_surface:
                    raise RuntimeError('duplicate owned surface windows; refusing ambiguous repair')
                by_surface[row['surface']] = row
    valid = {s['id'] for g in desired for s in g['surfaces']}
    bindings = {}
    for group in desired:
        session = sessions.get(group['key'])
        if session and tmux('has-session', '-t', session, check=False).returncode != 0:
            session = None
        if session:
            current_name = next((r['name'] for r in rows if r['session'] == session), group['name'])
            if current_name != group['name']:
                if tmux('has-session', '-t', '='+group['name'], check=False).returncode == 0:
                    group['name'] = current_name  # preserve an unowned restored shell
                else:
                    tmux('rename-session', '-t', session, group['name'])
                for row in rows:
                    if row['session'] == session:
                        row['name'] = group['name']
        for position, surface in enumerate(group['surfaces']):
            sid = surface['id']
            path = state_path(sid)
            state = load(path)
            queue = path.with_suffix('.queue')
            queue.mkdir(exist_ok=True)
            state.update(surface_id=sid, workspace_id=group['workspace_id'],
                         cmux_window_id=group['window_id'], cmux_pane_id=group['pane_id'],
                         title=surface['title'], grouped=True)
            atomic(path, state)
            start = shlex.join([sys.executable, str(ROOT / 'moshi-cmux-mirror.py'), str(path), str(queue)])
            row = by_surface.get(sid)
            fresh = row is None
            if not session:
                # New session's first window is a real mirror, never an unrelated shell.
                base_name = group['name']
                suffix = 2
                while tmux('has-session', '-t', '='+group['name'], check=False).returncode == 0:
                    group['name'] = f'{base_name}-{suffix}'
                    suffix += 1
                created = tmux('new-session', '-d', '-P', '-F', '#{session_id}\t#{window_id}\t#{pane_id}',
                               '-s', group['name'], '-n', surface['title'] or 'terminal', start).stdout.strip().split('\t')
                session, wid, pid = created
                tmux('set-option', '-t', session, OWNER, group['key'])
                tmux('set-option', '-t', session, 'base-index', '0')
                tmux('set-option', '-t', session, 'renumber-windows', 'off')
                tmux('set-option', '-t', session, 'remain-on-exit', 'on')
                sessions[group['key']] = session
                if row:
                    # Preserve the old pane ID (agent bindings) while moving between groups.
                    tmux('move-window', '-d', '-s', row['window'], '-t', session+':10000')
                    tmux('kill-window', '-t', wid)
                    row['session'] = session
                    row['index'] = '10000'
                else:
                    row = dict(session=session, name=group['name'], window=wid, pane=pid, dead='0', title=surface['title'], index=tmux('display-message', '-p', '-t', wid, '#{window_index}').stdout.strip())
                    by_surface[sid] = row
            if not row:
                wid, pid = tmux('new-window', '-d', '-P', '-F', '#{window_id}\t#{pane_id}',
                                '-t', session+':', '-n', surface['title'] or 'terminal', start).stdout.strip().split('\t')
                row = dict(session=session, name=group['name'], window=wid, pane=pid, dead='0', title=surface['title'], index=tmux('display-message', '-p', '-t', wid, '#{window_index}').stdout.strip())
                by_surface[sid] = row
            elif row['session'] != session:
                tmux('move-window', '-d', '-s', row['window'], '-t', session+':10000')
                row['session'] = session
                row['index'] = '10000'
            if fresh:
                tmux('set-option', '-w', '-t', row['window'], SURFACE, sid)
                tmux('set-option', '-w', '-t', row['window'], 'automatic-rename', 'off')
            if row['title'] != surface['title']:
                tmux('rename-window', '-t', row['window'], surface['title'] or 'terminal')
            if row['dead'] == '1':
                tmux('respawn-pane', '-t', row['pane'], start)
            state.update(tmux_session=next((r['name'] for r in rows if r['session'] == session), group['name']),
                         tmux_pane=row['pane'], tmux_window=row['window'])
            atomic(path, state)
            bindings[sid] = (path, queue, row)
        # Swap by stable IDs: renames/reordering never invalidate agent pane IDs.
        for position, surface in enumerate(group['surfaces']):
            wid = by_surface[surface['id']]['window']
            current = by_surface[surface['id']]['index']
            if current == str(position):
                continue
            dest = session+':'+str(position)
            occupant = next((r for r in by_surface.values() if r['session'] == session and r['index'] == str(position)), None)
            if occupant:
                tmux('swap-window', '-d', '-s', wid, '-t', dest)
                occupant['index'] = current
            else:
                tmux('move-window', '-d', '-s', wid, '-t', dest)
            by_surface[surface['id']]['index'] = str(position)
    # Never touch unowned sessions, and only prune after a successful complete tree read.
    for row in rows:
        if row['group'] and row['surface'] and row['surface'] not in valid:
            tmux('kill-window', '-t', row['window'])
    return bindings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--hook', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    payload = sys.stdin.read() if args.hook else ''
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        try:
            if args.dry_run:
                data = tree()
                print(json.dumps([{'session': g['name'], 'tabs': len(g['surfaces'])} for g in groups(data)], indent=2))
                return 0
            with (STATE / '.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = tree()
                bindings = sync(data)
                if args.hook:
                    sid = os.environ.get('CMUX_SURFACE_ID', '')
                    match = next((v for k,v in bindings.items() if k.lower() == sid.lower()), None)
                    if not match:
                        raise ValueError('hook surface absent from cmux tree')
                    path, queue, row = match
                    event = json.loads(payload)
                    state = load(path)
                    state.update(session_id=event.get('session_id'), cwd=event.get('cwd'), stale=event.get('hook_event_name') == 'SessionEnd')
                    atomic(path, state)
                    req = queue / ('req-'+uuid.uuid4().hex+'.json')
                    atomic(req, event)
            if args.hook:
                event_name = event.get('hook_event_name')
                needs_result = event_name in {'PermissionRequest', 'PreToolUse'}
                deadline = time.monotonic() + (130 if event_name == 'PermissionRequest' else 20 if needs_result else 15)
                done = req.with_suffix('.done')
                while time.monotonic() < deadline:
                    if done.exists():
                        code = int(done.read_text())
                        if needs_result:
                            sys.stdout.write(req.with_suffix('.out').read_text())
                        return code if needs_result else 0
                    time.sleep(.1)
                return 1 if needs_result else 0
            if not args.watch:
                print(json.dumps({'groups': len(groups(data)), 'surfaces': len(bindings)}))
                return 0
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print('moshi cmux groups: '+str(exc), file=sys.stderr)
            if not args.watch:
                return 1
        # One shared visibility sample avoids each idle mirror spawning tmux.
        until = time.monotonic() + 15
        while time.monotonic() < until:
            try:
                clients = tmux('list-clients', '-F', '#{pane_id}', check=False)
                atomic(STATE / 'visibility.json', {'updated': time.time(), 'panes': clients.stdout.splitlines()})
            except (OSError, subprocess.SubprocessError):
                pass  # mirrors fall back to a direct read when the sample is stale
            time.sleep(.25)


if __name__ == '__main__':
    raise SystemExit(main())
