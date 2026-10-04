#!/usr/bin/env python3
"""Resume only loaded top-level threads whose newest turn has this fatal error."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import resource
import socket
import sqlite3
import time

ERROR = 'application network permission was revoked'
PROMPT = ('Continue the existing task from its last checkpoint. The user authorized automatic '
          'continuation after the runtime error "application network permission was revoked". '
          'Preserve the original task scope, worktree ownership, model, permissions and approval '
          'requirements. Check tool outcomes before retrying; do not repeat completed actions.')
MIN_FILES = 4096


def raise_file_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = MIN_FILES if hard == resource.RLIM_INFINITY else min(MIN_FILES, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    return resource.getrlimit(resource.RLIMIT_NOFILE)


class Client:
    def __init__(self, home):
        import websocket
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(15)
        try:
            sock.connect(str(home / 'app-server-control/app-server-control.sock'))
            self.ws = websocket.create_connection('ws://localhost/', socket=sock, timeout=15)
        except BaseException:
            sock.close()
            raise
        self.counter = 0
        self.rpc('initialize', {'clientInfo': {'name': 'codex-network-recovery', 'version': '1.0'},
                               'capabilities': {'experimentalApi': True}})
        self.ws.send(json.dumps({'method': 'initialized'}))

    def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.ws.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv())
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError(f'{method}: {message["error"].get("code")}')
                return message['result']
            # Leave approval/server requests to their existing owner; never answer them.
        raise TimeoutError(method)

    def close(self):
        self.ws.close()


def failed_turn(path):
    """Use the canonical database path, not inherited session_meta IDs in child traces."""
    last = None
    with open(path) as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            payload = item.get('payload', {})
            if item.get('type') == 'event_msg' and payload.get('type') in (
                    'task_started', 'task_complete', 'turn_aborted'):
                last = payload
    if not last or last.get('type') != 'task_complete':
        return None
    if ERROR not in (last.get('error') or {}).get('message', ''):
        return None
    return last


def eligible(thread, failed, now):
    if thread.get('status', {}).get('type') != 'systemError':
        return False
    # Child assignments belong to their parent; do not revive retired/paused publishers.
    if thread.get('parentThreadId') or thread.get('canAcceptDirectInput') is False:
        return False
    age = now - failed.get('completed_at', 0)
    return 30 <= age <= 86400


def recover(client, thread_id, failed, state, now, dry_run=False):
    key = thread_id + ':' + failed['turn_id']
    attempts = state.setdefault('attempts', {})
    if key in attempts:
        return 'already-attempted'
    recent = [x for x in attempts.values() if x['thread'] == thread_id and now - x['time'] < 3600]
    if len(recent) >= 3:
        return 'rate-limited'
    thread = client.rpc('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']
    if thread.get('status', {}).get('type') != 'systemError':
        return 'skip'
    page = client.rpc('thread/turns/list', {'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc'})
    turns = page.get('data', [])
    if not turns or turns[0]['id'] != failed['turn_id']:
        return 'changed'
    if ERROR not in (turns[0].get('error') or {}).get('message', ''):
        return 'changed'
    if thread.get('parentThreadId') and thread.get('status', {}).get('type') == 'systemError':
        parent_id = thread['parentThreadId']
        parent = client.rpc('thread/read', {'threadId': parent_id, 'includeTurns': False})['thread']
        if parent.get('status', {}).get('type') != 'active':
            return 'skip'
        if parent.get('status', {}).get('activeFlags'):
            return 'skip'
        page = client.rpc('thread/turns/list', {'threadId': parent_id, 'limit': 1, 'sortDirection': 'desc'})
        turns = page.get('data', [])
        if not turns or turns[0].get('status') != 'inProgress':
            return 'skip'
        if dry_run:
            return 'would-notify-parent'
        attempts[key] = {'thread': thread_id, 'time': now, 'status': 'notifying-parent'}
        save_state(state)
        client.rpc('turn/steer', {'threadId': parent_id, 'expectedTurnId': turns[0]['id'],
            'input': [{'type': 'text', 'text': (
                'The user authorized recovery from application network permission revocation. '
                f'Your child {thread_id} stopped with that exact error. '
                'If its assignment is still active, tell it to continue from its checkpoint. '
                'Preserve intentionally paused or retired assignments and coordinate writers; '
                'do not duplicate completed work.'), 'text_elements': []}]})
        attempts[key]['status'] = 'parent-notified'
        save_state(state)
        return 'parent-notified'
    if not eligible(thread, failed, now):
        return 'skip'
    if dry_run:
        return 'would-resume'
    # Rejoin the same loaded runtime. Omit every configuration override.
    resumed = client.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True})['thread']
    if not eligible(resumed, failed, now):
        return 'changed'
    # Persist before send: a lost response must not duplicate a possibly accepted prompt.
    attempts[key] = {'thread': thread_id, 'time': now, 'status': 'sending'}
    save_state(state)
    result = client.rpc('turn/start', {'threadId': thread_id,
                       'clientUserMessageId': 'network-recovery-' + failed['turn_id'],
                       'input': [{'type': 'text', 'text': PROMPT, 'text_elements': []}]})
    attempts[key]['status'] = 'accepted'
    attempts[key]['new_turn'] = result['turn']['id']
    save_state(state)
    return 'resumed'


STATE_PATH = None


def save_state(state):
    if STATE_PATH is None:
        return
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_PATH)


def main():
    global STATE_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    raise_file_limit()
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    root = Path.home() / '.local/state/codex-network-recovery'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_PATH = root / 'state.json'
    with (root / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {'attempts': {}}
        now = int(time.time())
        db = sqlite3.connect(f'file:{home}/state_5.sqlite?mode=ro', uri=True)
        rows = db.execute('SELECT id, rollout_path FROM threads WHERE archived=0 AND updated_at>? '
                          'ORDER BY updated_at DESC LIMIT 200', (now - 86400,)).fetchall()
        db.close()
        candidates = []
        for thread_id, path in rows:
            try:
                failed = failed_turn(path)
            except (OSError, ValueError):
                continue
            if failed and 30 <= now - failed.get('completed_at', 0) <= 86400:
                candidates.append((thread_id, failed))
        if not candidates:
            return
        client = Client(home)
        try:
            for thread_id, failed in candidates:
                try:
                    outcome = recover(client, thread_id, failed, state, now, args.dry_run)
                except (RuntimeError, TimeoutError, OSError) as error:
                    print(json.dumps({"thread": thread_id, "outcome": "rpc-error",
                                      "error_type": type(error).__name__}), flush=True)
                    continue
                if outcome in ('resumed', 'would-resume', 'rate-limited', 'parent-notified', 'would-notify-parent'):
                    print(json.dumps({'thread': thread_id, 'failed_turn': failed['turn_id'], 'outcome': outcome}), flush=True)
        finally:
            client.close()


if __name__ == '__main__':
    main()
