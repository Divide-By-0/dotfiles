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
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('MOSHI_GROUP_STATE_DIR', str(Path.home() / '.claude/hooks/moshi-cmux-groups')))
OWNER = '@moshi_cmux_group'
SURFACE = '@moshi_cmux_surface'
NON_TERMINAL_TYPES = {'browser', 'simulator', 'agent-session', 'markdown', 'filepreview'}


def command(args, *, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=10)


def tmux(*args, check=True):
    cmd = [os.environ.get('TMUX_BIN') or shutil.which('tmux') or '/opt/homebrew/bin/tmux']
    if os.environ.get('TMUX_SOCKET'):
        cmd += ['-L', os.environ['TMUX_SOCKET']]
    return command(cmd + list(args), check=check)


def cmux_rpc(method, params):
    exe = os.environ.get('CMUX_BIN') or shutil.which('cmux') or '/opt/homebrew/bin/cmux'
    data = json.loads(command([exe, 'rpc', method, json.dumps(params)]).stdout)
    if not isinstance(data, dict):
        raise ValueError('unsupported cmux RPC response: expected object')
    if data.get('error') or data.get('ok') is False:
        raise ValueError('cmux '+method+' returned an error; preserving existing panes')
    return data


def tree():
    data = cmux_rpc('system.tree', {})
    if not isinstance(data, dict) or not isinstance(data.get('windows'), list):
        raise ValueError('cmux returned no authoritative window list')
    return data


def validate_tree(data):
    """Reject incomplete/changed schemas before any tmux mutation or pruning."""
    seen = set()
    def children(node, key):
        if not isinstance(node, dict) or not isinstance(node.get(key), list):
            raise ValueError('unsupported cmux tree: missing '+key+' list')
        return node[key]
    def identity(node):
        if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']:
            raise ValueError('unsupported cmux tree: missing stable ID')
    for window in children(data, 'windows'):
        identity(window)
        for workspace in children(window, 'workspaces'):
            identity(workspace)
            if not isinstance(workspace.get('title', ''), str):
                raise ValueError('unsupported cmux tree: workspace title')
            for pane in children(workspace, 'panes'):
                identity(pane)
                for surface in children(pane, 'surfaces'):
                    identity(surface)
                    if surface['id'] in seen:
                        raise ValueError('unsupported cmux tree: duplicate surface ID')
                    seen.add(surface['id'])
                    if not isinstance(surface.get('type'), str) or not surface['type']:
                        raise ValueError('unsupported cmux tree: missing surface type')
                    if surface['type'] == 'terminal' and (type(surface.get('index_in_pane')) is not int or
                            not isinstance(surface.get('title'), str)):
                        raise ValueError('unsupported cmux tree: terminal index/title')


def window_title(title):
    # tmux rejects tabs/newlines in names; keep the state file's original title.
    return re.sub(r'[\x00-\x1f\x7f]', ' ', title).strip() or 'terminal'


def groups(data):
    validate_tree(data)
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
                slug = re.sub(r'[^a-z0-9]+', '-', workspace.get('title', '').lower()).strip('-')[:28] or 'workspace'
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
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory():
    result = tmux('list-windows', '-a', '-F',
                  '#{session_id}\t#{session_name}\t#{'+OWNER+'}\t#{window_id}\t#{window_index}\t#{'+SURFACE+'}\t#{pane_id}\t#{pane_dead}\t#{remain-on-exit}\t#{window_name}', check=False)
    if result.returncode and not ('no server running on ' in result.stderr or
                                  ('error connecting to ' in result.stderr and 'No such file or directory' in result.stderr)):
        raise RuntimeError('tmux inventory failed; preserving existing panes: '+result.stderr.strip())
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split('\t', 9)
        if len(fields) != 10:
            raise RuntimeError('unsupported tmux inventory format; preserving existing panes')
        rows.append(dict(zip(['session', 'name', 'group', 'window', 'index', 'surface', 'pane', 'dead', 'keep', 'title'], fields)))
    return rows


IDLE_SHELLS = {'zsh', 'bash', 'sh', 'fish', 'login', 'secretty'}


def pane_is_idle_shell(pane):
    """True only if the pane's whole process tree is shells with nothing running.

    Every pane here is secretty -> zsh, so pane_current_command alone cannot see
    an agent started inside it; walk the real process tree instead.
    """
    root = tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False).stdout.strip()
    if not root.isdigit():
        return False
    children = {}
    for line in command(['ps', '-A', '-o', 'pid=,ppid=,comm=']).stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            children.setdefault(parts[1], []).append((parts[0], parts[2]))
    names = {os.path.basename(command(['ps', '-o', 'comm=', '-p', root], check=False).stdout.strip())}
    todo = [root]
    while todo:
        for pid, comm in children.get(todo.pop(), []):
            names.add(os.path.basename(comm))
            todo.append(pid)
    return all(name.lstrip('-') in IDLE_SHELLS for name in names)


def adopt_new_windows(desired, rows, by_surface):
    """Turn a tab opened on the phone into a real cmux tab.

    A window in one of our sessions without a surface binding was created by a
    tmux client (Moshi's new-tab, prefix+c), not by this bridge. Create a cmux
    terminal tab in the same cmux pane and bind that window to it, keeping the
    window and pane IDs the phone is already looking at. Returns the pane IDs
    whose shells must be replaced by the new surface's mirror.
    """
    by_key = {g['key']: g for g in desired}
    respawn = set()
    for row in rows:
        group = by_key.get(row['group'])
        if not group or row['surface']:
            continue
        start = tmux('display-message', '-p', '-t', row['pane'], '#{pane_start_command}').stdout
        if 'moshi-cmux-mirror.py' in start or 'moshi-cmux --mirror' in start:
            continue  # our launcher may briefly look like an idle shell during runtime discovery
        # REASON: adoption replaces the pane's process with a mirror
        # (respawn-pane -k), so only a pane that is nothing but an idle login
        # shell may be adopted. On 2026-09-26 a Claude session the user had
        # started in a phone-made tab was killed this way. A busy window is left
        # alone, with no cmux tab, until its program exits. This also skips
        # mirrors whose surface-option write failed (their process is python).
        if not pane_is_idle_shell(row['pane']):
            continue
        created = cmux_rpc('surface.create', {'type': 'terminal', 'pane_id': group['pane_id'],
                                              'workspace_id': group['workspace_id'],
                                              'window_id': group['window_id'], 'focus': False})
        sid = created.get('surface_id')
        if not sid:
            raise ValueError('cmux surface.create returned no surface_id')
        # The tree was read before this tab existed; append it so this same pass
        # binds it at the end of the strip, where cmux and tmux both put new tabs.
        group['surfaces'].append(dict(id=sid, title=row['title'] or 'terminal', type='terminal',
                                      index_in_pane=len(group['surfaces'])))
        tmux('set-option', '-w', '-t', row['window'], SURFACE, sid)
        tmux('set-option', '-w', '-t', row['window'], 'automatic-rename', 'off')
        row['surface'] = sid
        by_surface[sid] = row
        respawn.add(row['pane'])
    return respawn


def move_owned_window(row, session):
    # Let tmux choose a free slot. A fixed staging index collides when multiple
    # surviving surface windows move into a rebuilt cmux strip in the same pass.
    tmux('move-window', '-d', '-s', row['window'], '-t', session+':')
    row['session'] = session
    row['index'] = tmux('display-message', '-p', '-t', row['window'], '#{window_index}').stdout.strip()


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
    respawn = adopt_new_windows(desired, rows, by_surface)
    valid = {s['id'] for g in desired for s in g['surfaces']}
    # Future non-terminal types must not stop known terminals from working or
    # make an existing mirror look deleted. Preserve unfamiliar surface IDs.
    valid.update(s['id'] for w in data['windows'] for ws in w['workspaces']
                 for p in ws['panes'] for s in p['surfaces']
                 if s['type'] != 'terminal' and s['type'] not in NON_TERMINAL_TYPES)
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
            launcher = os.environ.get('MOSHI_CMUX_LAUNCHER', str(Path.home() / '.local/bin/moshi-cmux'))
            start = shlex.join([launcher, '--mirror', str(path), str(queue)])
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
                               '-s', group['name'], '-n', window_title(surface['title']), start).stdout.strip().split('\t')
                session, wid, pid = created
                tmux('set-option', '-t', session, OWNER, group['key'])
                tmux('set-option', '-t', session, 'base-index', '0')
                tmux('set-option', '-t', session, 'renumber-windows', 'off')
                tmux('set-option', '-t', session, 'remain-on-exit', 'on')
                sessions[group['key']] = session
                if row:
                    # Preserve the old pane ID (agent bindings) while moving between groups.
                    try:
                        move_owned_window(row, session)
                    finally:
                        # A failed move must not leave a duplicate temporary
                        # mirror occupying index 0 and block the next retry.
                        tmux('kill-window', '-t', wid)
                else:
                    row = dict(session=session, name=group['name'], window=wid, pane=pid, dead='0', title=window_title(surface['title']), index=tmux('display-message', '-p', '-t', wid, '#{window_index}').stdout.strip())
                    by_surface[sid] = row
            if not row:
                wid, pid = tmux('new-window', '-d', '-P', '-F', '#{window_id}\t#{pane_id}',
                                '-t', session+':', '-n', window_title(surface['title']), start).stdout.strip().split('\t')
                row = dict(session=session, name=group['name'], window=wid, pane=pid, dead='0', title=window_title(surface['title']), index=tmux('display-message', '-p', '-t', wid, '#{window_index}').stdout.strip())
                by_surface[sid] = row
            elif row['session'] != session:
                move_owned_window(row, session)
            if fresh:
                tmux('set-option', '-w', '-t', row['window'], SURFACE, sid)
                tmux('set-option', '-w', '-t', row['window'], 'automatic-rename', 'off')
            # REASON: remain-on-exit is a window option. The session-level
            # set-option above only reaches the session's first window, so every
            # other mirror window closed when its mirror exited and came back with
            # a new ID under the phone (42 of 51 on 2026-09-27). Keep each window.
            if row.get('keep') != 'on':
                tmux('set-option', '-w', '-t', row['window'], 'remain-on-exit', 'on')
                row['keep'] = 'on'
            if row['title'] != window_title(surface['title']):
                tmux('rename-window', '-t', row['window'], window_title(surface['title']))
            if row['dead'] == '1' or row['pane'] in respawn:
                tmux('respawn-pane', '-k', '-t', row['pane'], start)
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
            # A phone-created window can occupy this index without a surface
            # binding. Swap it intact instead of failing a move into its slot.
            # display-message succeeds for a missing index by falling back to
            # the selected window. Enumerate actual slots instead (AGENTS-528).
            slots = dict(line.split('\t', 1) for line in tmux(
                'list-windows', '-t', session, '-F', '#{window_index}\t#{window_id}').stdout.splitlines())
            target = slots.get(str(position))
            if target:
                swap = f'swap-window -d -s {wid} -t {target}'
                # -d retains the selected *index* when the destination is
                # active. Follow its stable ID instead, atomically in tmux's
                # command queue so a phone swipe cannot race a Python readback.
                tmux('if-shell', '-F', '-t', target, '#{window_active}',
                     swap + f' ; select-window -t {target}', swap)
                if occupant:
                    occupant['index'] = current
            else:
                tmux('move-window', '-d', '-s', wid, '-t', dest)
            by_surface[surface['id']]['index'] = str(position)
    # Never touch unowned sessions, and only prune after a successful complete tree read.
    for row in rows:
        if row['group'] and row['surface'] and row['surface'] not in valid:
            tmux('kill-window', '-t', row['window'])
    return bindings


def watch_visibility(stop):
    """One sampler, independent of slow topology RPCs and the sync lock."""
    while not stop.is_set():
        interval = 1.0
        try:
            clients = tmux('list-clients', '-F', '#{pane_id} #{client_activity}', check=False)
            rows = [line.split() for line in clients.stdout.splitlines() if line.strip()]
            activity = max((int(r[1]) for r in rows if len(r) > 1 and r[1].isdigit()), default=0)
            atomic(STATE / 'visibility.json', {'updated': time.time(), 'panes': [r[0] for r in rows], 'activity': activity})
            if rows and time.time() - activity < 300:
                interval = .25
        except (OSError, subprocess.SubprocessError):
            pass  # grouped mirrors stay idle until a fresh sample is available
        stop.wait(interval)


def health(data):
    surfaces = {s['id'] for group in groups(data) for s in group['surfaces']}
    rows = [r for r in inventory() if r['group'] and r['surface'] in surfaces]
    dead = sum(r['dead'] == '1' for r in rows)
    missing = len(surfaces - {r['surface'] for r in rows})
    duplicates = len(rows) - len({r['surface'] for r in rows})
    sample = load(STATE / 'visibility.json')
    updated = sample.get('updated')
    age = time.time() - updated if isinstance(updated, (int, float)) and math.isfinite(updated) else None
    return dict(healthy=not dead and not missing and not duplicates and age is not None and 0 <= age < 5,
                runtime=sys.executable, terminal_surfaces=len(surfaces),
                mirrors=len(rows), dead_mirrors=dead, unmapped_surfaces=missing, duplicate_mirrors=duplicates,
                visibility_age_seconds=round(age, 2) if age is not None else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--hook', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--event', action='store_true', help='read only the hook event name')
    parser.add_argument('--status', action='store_true', help='read bridge health without repairing panes')
    args = parser.parse_args()
    if args.event:
        print(json.load(sys.stdin).get('hook_event_name') or '')
        return 0
    payload = sys.stdin.read() if args.hook else ''
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.watch and not args.dry_run:
        threading.Thread(target=watch_visibility, args=(threading.Event(),), daemon=True).start()
    while True:
        try:
            if args.status:
                status = health(tree())
                print(json.dumps(status))
                return 0 if status['healthy'] else 1
            if args.dry_run:
                data = tree()
                print(json.dumps([{'session': g['name'], 'tabs': len(g['surfaces'])} for g in groups(data)], indent=2))
                return 0
            with (STATE / '.lock').open('w') as lock:
                if args.watch or args.hook:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                else:
                    # REASON: plain one-shot runs come from the after-new-window
                    # hook, which also fires for the windows sync() itself creates.
                    # Queueing them on the lock piled up ~25 waiting syncs on
                    # 2026-09-27. A running sync (or the 15 s daemon) covers the
                    # new window, so skip instead of waiting.
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return 0
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
        time.sleep(15)



if __name__ == '__main__':
    raise SystemExit(main())
