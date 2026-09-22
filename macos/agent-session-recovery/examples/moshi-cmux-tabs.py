#!/usr/bin/env python3
"""Minimal cmux tab strip -> tmux session for Moshi (Python stdlib only).

Run from a cmux terminal: python3 moshi-cmux-tabs.py
Open the printed session in Moshi; prefix+n/p switches sibling terminals.
Snapshot only: rerun after changing tabs. No hooks, agents or services installed.
Plain-text display and basic keyboard input; no colors, mouse or wide-cell layout.
"""
import argparse
import codecs
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import termios
import tty
import uuid


def tmux(*args):
    command = [os.environ.get('TMUX_BIN', 'tmux')]
    if os.environ.get('TMUX_SOCKET'):
        command += ['-L', os.environ['TMUX_SOCKET']]
    return subprocess.check_output(command + list(args), text=True, timeout=10).strip()


def rpc(method, **params):
    output = subprocess.check_output(
        [os.environ.get('CMUX_BIN', 'cmux'), 'rpc', method, json.dumps(params)],
        text=True, timeout=10)
    return json.loads(output)


def siblings(tree, surface_id):
    for window in tree['windows']:
        for workspace in window['workspaces']:
            for pane in workspace['panes']:
                if any(s['id'] == surface_id for s in pane['surfaces']):
                    return sorted((s for s in pane['surfaces'] if s['type'] == 'terminal'),
                                  key=lambda s: s['index_in_pane'])
    raise ValueError('surface not found; run from a cmux terminal or pass --surface UUID')


def create(surface_id):
    surfaces = siblings(rpc('system.tree'), surface_id)
    if not surfaces:
        raise ValueError('this tab strip has no terminal tabs')
    name = 'cmux-demo-' + uuid.uuid4().hex[:12]
    session = None
    try:
        for index, surface in enumerate(surfaces):
            # Pass connection settings explicitly: an existing tmux server may
            # have a different environment from the shell launching this demo.
            settings = [f'{key}={os.environ[key]}' for key in
                        ('CMUX_BIN', 'CMUX_SOCKET_PATH') if key in os.environ]
            command = shlex.join(['env', *settings, sys.executable,
                                  str(Path(__file__).resolve()), '--mirror', surface['id']])
            if index == 0:
                session = tmux('new-session', '-d', '-P', '-F', '#{session_id}',
                               '-s', name, '-n', 'tab-1', command)
                tmux('set-option', '-t', session, 'prefix', 'C-b')
            else:
                tmux('new-window', '-d', '-t', session + ':', '-n', f'tab-{index + 1}', command)
        return name
    except Exception:
        if session:
            tmux('kill-session', '-t', session)
        raise


def frame(grid, columns, rows):
    cells = [[' '] * columns for _ in range(rows)]
    for span in grid.get('row_spans', []):
        row, col = span['row'], span['column']
        if 0 <= row < rows:
            for offset, char in enumerate(span['text']):
                if 0 <= col + offset < columns:
                    cells[row][col + offset] = char if char.isprintable() else ' '
    return '\x1b[H\x1b[J' + '\r\n'.join(''.join(row) for row in cells)


def mirror(surface_id):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending = ''
    keys = {'\r': 'enter', '\n': 'enter', '\t': 'tab', '\x7f': 'backspace',
            '\x1b[A': 'up', '\x1b[B': 'down', '\x1b[C': 'right', '\x1b[D': 'left'}
    try:
        tty.setraw(fd)
        sys.stdout.write('\x1b[?1049h\x1b[?25l')
        while True:
            ready = select.select([fd], [], [], 0.15)[0]
            if ready:
                data = os.read(fd, 4096)
                if not data:
                    break
                pending += decoder.decode(data)
            while pending:
                # Keep escape sequences intact even when split across reads.
                if ready and any(k.startswith(pending) and k != pending for k in keys):
                    break
                key = next((k for k in keys if pending.startswith(k)), pending[0])
                pending = pending[len(key):]
                name = keys.get(key)
                if key == '\x1b':
                    name = 'escape'
                elif len(key) == 1 and 1 <= ord(key) <= 26 and not name:
                    name = 'ctrl+' + chr(ord(key) + 96)
                if name:
                    rpc('surface.send_key', surface_id=surface_id, key=name)
                else:
                    rpc('surface.send_text', surface_id=surface_id, text=key)
            size = os.get_terminal_size(sys.stdout.fileno())
            replay = rpc('terminal.replay', surface_id=surface_id,
                         columns=size.columns, rows=size.lines)
            grid = replay.get('render_grid')
            if not grid:
                continue  # A newly created cmux terminal may not have a grid yet.
            sys.stdout.write(frame(grid, size.columns, size.lines))
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write('\x1b[?25h\x1b[?1049l')
        sys.stdout.flush()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--surface', default=os.environ.get('CMUX_SURFACE_ID'))
    parser.add_argument('--mirror', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mirror:
        mirror(args.mirror)
    elif args.surface:
        print(create(args.surface))
    else:
        parser.error('run inside cmux or pass --surface UUID')
