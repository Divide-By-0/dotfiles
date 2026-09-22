#!/usr/bin/env python3
"""Minimal cmux -> tmux -> Moshi bridge/reproducer.

Run ``hook`` from a Claude SessionStart hook in a cmux terminal. The hook
creates one shadow tmux session for the current cmux surface, then replays the
same SessionStart event from inside that tmux pane so Moshi can discover it.
The pane mirrors cmux output and forwards basic keyboard input.

This intentionally keeps the problematic mapping: one tmux session per cmux
surface. It proves the bridge works and isolates why Moshi cannot reconstruct
cmux's workspace/vertical-tab grouping from the tmux context it receives.
"""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import uuid
from pathlib import Path

CMUX = os.environ.get("CMUX_BIN") or shutil.which("cmux") or "cmux"
MOSHI = os.environ.get("MOSHI_BIN") or shutil.which("moshi") or "moshi"
TMUX = os.environ.get("TMUX_BIN") or shutil.which("tmux") or "tmux"

KEYS = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x7f": "backspace",
    b"\r": "enter",
    b"\n": "enter",
    b"\t": "tab",
    b"\x1b": "escape",
}


def run_tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    command = [TMUX]
    socket = os.environ.get("TMUX_SOCKET")
    if socket:
        command.extend(["-L", socket])
    command.extend(args)
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def rpc(method: str, params: dict) -> dict:
    result = subprocess.run(
        [CMUX, "rpc", method, json.dumps(params)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


def surface_key(surface_id: str) -> str:
    compact = re.sub(r"[^a-z0-9]", "", surface_id.lower())
    return (compact or "unknown")[:12]


def bind_hook() -> int:
    payload = sys.stdin.read()
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        print("expected a Claude hook JSON payload", file=sys.stderr)
        return 2

    if event.get("hook_event_name") != "SessionStart":
        return 0

    surface_id = os.environ.get("CMUX_SURFACE_ID", "")
    workspace_id = os.environ.get("CMUX_WORKSPACE_ID", "")
    if not surface_id:
        print("CMUX_SURFACE_ID is missing", file=sys.stderr)
        return 2

    key = surface_key(surface_id)
    session = f"moshi-cmux-mre-{key}"
    if run_tmux("has-session", "-t", f"{session}:").returncode == 0:
        return 0

    state_dir = Path(tempfile.gettempdir()) / "moshi-cmux-mre"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload_path = state_dir / f"{key}.json"
    payload_path.write_text(payload)
    payload_path.chmod(0o600)

    command = shlex.join(
        [sys.executable, str(Path(__file__).resolve()), "mirror", surface_id, str(payload_path)]
    )
    created = run_tmux("new-session", "-d", "-s", session, command)
    if created.returncode != 0:
        print(created.stderr.strip() or "tmux new-session failed", file=sys.stderr)
        return created.returncode or 1

    # The grouping information exists, but Moshi only observes the shadow tmux
    # session. It therefore sees each surface as an unrelated top-level item.
    run_tmux("set-option", "-t", f"{session}:", "@cmux_surface_id", surface_id)
    run_tmux("set-option", "-t", f"{session}:", "@cmux_workspace_id", workspace_id)
    return 0


def paint(grid: dict, cols: int, rows: int) -> str:
    cells = [[" "] * cols for _ in range(rows)]
    for span in grid.get("row_spans") or []:
        row = int(span.get("row") or 0)
        col = int(span.get("column") or 0)
        if not 0 <= row < rows:
            continue
        for offset, char in enumerate(span.get("text") or ""):
            column = col + offset
            if 0 <= column < cols:
                cells[row][column] = char

    frame = "\x1b[H\x1b[J" + "\r\n".join("".join(row) for row in cells)
    cursor = grid.get("cursor") or {}
    if cursor.get("visible", True):
        row = int(cursor.get("row") or 0)
        col = int(cursor.get("column") or 0)
        if 0 <= row < rows and 0 <= col < cols:
            frame += f"\x1b[{row + 1};{col + 1}H\x1b[?25h"
    return frame


def split_keys(data: bytes) -> list[bytes]:
    keys: list[bytes] = []
    index = 0
    while index < len(data):
        if data[index] != 0x1B:
            keys.append(data[index : index + 1])
            index += 1
            continue
        end = index + 1
        while end < len(data) and end - index < 8:
            byte = data[end]
            end += 1
            if 0x40 <= byte <= 0x7E and byte not in (0x5B, 0x4F):
                break
        keys.append(data[index:end])
        index = end
    return keys


def forward(surface_id: str, key: bytes) -> None:
    if key in KEYS:
        rpc("surface.send_key", {"surface_id": surface_id, "key": KEYS[key]})
        return
    if len(key) == 1 and 1 <= key[0] <= 26:
        rpc(
            "surface.send_key",
            {"surface_id": surface_id, "key": f"ctrl+{chr(key[0] + 96)}"},
        )
        return
    try:
        text = key.decode("utf-8")
    except UnicodeDecodeError:
        return
    rpc("surface.send_text", {"surface_id": surface_id, "text": text})


def mirror(surface_id: str, payload_path: Path, fps: float = 8.0) -> int:
    payload = payload_path.read_text()
    payload_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["MOSHI_DISABLE_PARENT_TERMINAL_LOOKUP"] = "1"
    bound = subprocess.run(
        [MOSHI, "claude-hook"],
        input=payload,
        text=True,
        env=env,
        check=False,
    )
    if bound.returncode != 0:
        return bound.returncode

    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        print("mirror must run in a tty", file=sys.stderr)
        return 2

    old_termios = termios.tcgetattr(sys.stdin.fileno())
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    tty.setraw(sys.stdin.fileno())
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()

    client_id = "moshi-example-" + uuid.uuid4().hex
    try:
        while not stopping:
            size = os.get_terminal_size(sys.stdout.fileno())
            cols, rows = max(20, size.columns), max(10, size.lines)
            ready, _, _ = select.select([sys.stdin], [], [], 1.0 / max(fps, 1.0))
            if ready:
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                for key in split_keys(data):
                    forward(surface_id, key)

            replay = rpc(
                "terminal.replay",
                {"surface_id": surface_id, "client_id": client_id,
                 "viewport_columns": cols, "viewport_rows": rows},
            )
            grid = replay.get("render_grid") or {}
            if grid and grid.get("columns", cols) <= cols and grid.get("rows", rows) <= rows:
                sys.stdout.write(paint(grid, cols, rows))
                sys.stdout.flush()
    finally:
        rpc("terminal.viewport", {"surface_id": surface_id, "client_id": client_id, "clear": True})
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "hook":
        return bind_hook()
    if len(sys.argv) == 4 and sys.argv[1] == "mirror":
        return mirror(sys.argv[2], Path(sys.argv[3]))
    print(f"usage: {Path(sys.argv[0]).name} hook", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
