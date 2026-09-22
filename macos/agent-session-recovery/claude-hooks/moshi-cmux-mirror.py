#!/usr/bin/env python3
"""Live-mirror a cmux surface into this tty (Moshi/tmux ghost pane).

Phone clients attach to the ghost tmux session. Previously that pane only
showed a status page, so Moshi users saw nonsense and could not drive the
real Claude/Codex TUI running in cmux/Ghostty.

This process:
  1. Resizes the cmux PTY to match this pane (SIGWINCH-aware).
  2. Redraws terminal.replay row_spans into this tty.
  3. Forwards keystrokes to surface.send_text / surface.send_key.
  4. Keeps draining the Moshi hook queue so bindings stay correct.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
import uuid
from pathlib import Path

CMUX = os.environ.get("CMUX_BIN") or shutil.which("cmux") or "/opt/homebrew/bin/cmux"
MOSHI = os.environ.get("MOSHI_BIN") or shutil.which("moshi") or "/opt/homebrew/bin/moshi"
LOG = Path(os.environ.get("MOSHI_LOG_PATH", str(Path.home() / ".claude/hooks/moshi-sessionstart.log")))
VIEWPORT_CLIENT_ID = "moshi-tmux-" + uuid.uuid4().hex

# Special keys → cmux surface.send_key names (when recognized).
KEY_MAP = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[5~": "pageup",
    b"\x1b[6~": "pagedown",
    b"\x1b[3~": "delete",
    b"\x7f": "backspace",
    b"\x08": "backspace",
    b"\x1b": "escape",
    b"\t": "tab",
    b"\r": "enter",
    b"\n": "enter",
}


def log(msg: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(f"---- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ----\n{msg}\n")
    except OSError:
        pass


def rpc(method: str, params: dict | None = None) -> dict:
    args = [CMUX, "rpc", method]
    if params is not None:
        args.append(json.dumps(params))
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True, timeout=5)
        return json.loads(out) if out.strip() else {}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return {"_error": str(e)}


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def term_size() -> tuple[int, int]:
    size = os.get_terminal_size(sys.stdout.fileno())
    # Keep a usable minimum so Claude/Codex TUIs can draw.
    cols = max(20, size.columns)
    rows = max(10, size.lines)
    return cols, rows


def clear_viewport(surface_id: str) -> None:
    rpc(
        "terminal.viewport",
        {"surface_id": surface_id, "client_id": VIEWPORT_CLIENT_ID, "clear": True},
    )


def replay_viewport(surface_id: str, cols: int, rows: int) -> dict:
    # columns/rows are response fields, not resize parameters. Report the phone
    # dimensions with replay so Ghostty reflows and the real TUI gets SIGWINCH.
    # Replay reports expire if this process dies; terminal.viewport reports are
    # sticky and can leave the desktop pinned after an unclean mirror exit.
    return rpc("terminal.replay", {
        "surface_id": surface_id,
        "client_id": VIEWPORT_CLIENT_ID,
        "viewport_columns": cols,
        "viewport_rows": rows,
    })


def grid_fits(grid: dict, cols: int, rows: int) -> bool:
    # A resize may return a stale desktop frame before the new grid is ready.
    # Wait for reflow instead of silently cropping it. Smaller shared viewports
    # are valid (another attached client can own the minimum terminal size).
    return int(grid.get("columns", cols)) <= cols and int(grid.get("rows", rows)) <= rows


def send_text(surface_id: str, text: str) -> None:
    if not text:
        return
    rpc("surface.send_text", {"surface_id": surface_id, "text": text})


def send_key(surface_id: str, key: str) -> None:
    rpc("surface.send_key", {"surface_id": surface_id, "key": key})


def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not value.startswith("#"):
        return None
    h = value[1:]
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def style_ansi(style: dict | None) -> str:
    if not style:
        return ""
    parts: list[str] = []
    fg = style.get("foreground") or style.get("fg")
    bg = style.get("background") or style.get("bg")
    if isinstance(fg, str):
        rgb = _hex_to_rgb(fg)
        if rgb:
            parts.append(f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}")
    elif isinstance(fg, dict) and "r" in fg:
        parts.append(f"38;2;{fg['r']};{fg['g']};{fg['b']}")
    elif isinstance(fg, int) and 0 <= fg <= 255:
        parts.append(f"38;5;{fg}")
    if isinstance(bg, str):
        rgb = _hex_to_rgb(bg)
        if rgb:
            parts.append(f"48;2;{rgb[0]};{rgb[1]};{rgb[2]}")
    elif isinstance(bg, dict) and "r" in bg:
        parts.append(f"48;2;{bg['r']};{bg['g']};{bg['b']}")
    elif isinstance(bg, int) and 0 <= bg <= 255:
        parts.append(f"48;5;{bg}")
    if style.get("bold"):
        parts.append("1")
    if style.get("faint") or style.get("dim"):
        parts.append("2")
    if style.get("italic"):
        parts.append("3")
    if style.get("underline"):
        parts.append("4")
    if style.get("blink"):
        parts.append("5")
    if style.get("inverse") or style.get("reverse"):
        parts.append("7")
    if style.get("invisible"):
        parts.append("8")
    if style.get("strikethrough"):
        parts.append("9")
    return ("\x1b[" + ";".join(parts) + "m") if parts else ""


def build_frame(grid: dict, view_cols: int, view_rows: int) -> str:
    """Paint row_spans into a fixed-size character frame."""
    spans = grid.get("row_spans") or []
    style_list = grid.get("styles") or []
    styles = {
        s.get("id"): s
        for s in style_list
        if isinstance(s, dict) and s.get("id") is not None
    }

    lines = [[" "] * view_cols for _ in range(view_rows)]
    line_styles: list[list[str | None]] = [[None] * view_cols for _ in range(view_rows)]

    for span in spans:
        row = int(span.get("row") or 0)
        col = int(span.get("column") or 0)
        text = span.get("text") or ""
        if row < 0 or row >= view_rows:
            continue
        sid = span.get("style_id")
        style = styles.get(sid) if sid is not None else None
        if style is None and isinstance(sid, int) and 0 <= sid < len(style_list):
            cand = style_list[sid]
            if isinstance(cand, dict):
                style = cand
        ansi = style_ansi(style)
        # Prefer cell_width when present (CJK / wide glyphs).
        width = int(span.get("cell_width") or len(text))
        # Paint character by character; wide glyphs may be a single codepoint.
        if len(text) == 1 and width > 1:
            if 0 <= col < view_cols:
                lines[row][col] = text
                line_styles[row][col] = ansi
                for k in range(1, width):
                    c = col + k
                    if 0 <= c < view_cols:
                        lines[row][c] = ""
                        line_styles[row][c] = ansi
        else:
            for i, ch in enumerate(text):
                c = col + i
                if 0 <= c < view_cols:
                    lines[row][c] = ch
                    line_styles[row][c] = ansi

    cursor = grid.get("cursor") or {}
    out: list[str] = ["\x1b[H\x1b[J"]  # home + clear
    for r in range(view_rows):
        current = None
        buf: list[str] = []
        for c in range(view_cols):
            ch = lines[r][c]
            if ch == "":
                continue  # wide-glyph spacer
            a = line_styles[r][c]
            if a != current:
                if current:
                    buf.append("\x1b[0m")
                if a:
                    buf.append(a)
                current = a
            buf.append(ch)
        if current:
            buf.append("\x1b[0m")
        out.append("".join(buf))
        if r != view_rows - 1:
            out.append("\r\n")

    if cursor.get("visible", True):
        cr = int(cursor.get("row") or 0)
        cc = int(cursor.get("column") or 0)
        if 0 <= cr < view_rows and 0 <= cc < view_cols:
            out.append(f"\x1b[{cr + 1};{cc + 1}H")
            out.append("\x1b[?25h")
        else:
            out.append("\x1b[?25l")
    else:
        out.append("\x1b[?25l")
    return "".join(out)


def drain_queue(queue_dir: Path) -> None:
    if not queue_dir.is_dir():
        return
    for req in sorted(queue_dir.glob("req-*.json")):
        base = str(req)[:-5]
        donef = Path(base + ".done")
        outf = Path(base + ".out")
        try:
            env = os.environ.copy()
            env["MOSHI_DISABLE_PARENT_TERMINAL_LOOKUP"] = "1"
            with req.open("rb") as stdin_f, outf.open("wb") as stdout_f, LOG.open("ab") as log_f:
                proc = subprocess.run(
                    [MOSHI, "claude-hook"],
                    stdin=stdin_f,
                    stdout=stdout_f,
                    stderr=log_f,
                    env=env,
                    check=False,
                )
            donef.write_text(str(proc.returncode))
        except OSError as e:
            log(f"queue drain error {req}: {e}")
            try:
                donef.write_text("1")
            except OSError:
                pass
        try:
            req.unlink()
        except OSError:
            pass
    # Cleanup stale artifacts
    now = time.time()
    for p in queue_dir.glob("req-*.*"):
        try:
            if now - p.stat().st_mtime > 60 and p.suffix in {".done", ".out", ".err"}:
                p.unlink()
        except OSError:
            pass


def read_keys(buf: bytearray) -> list[bytes]:
    """Split raw stdin bytes into key sequences."""
    keys: list[bytes] = []
    i = 0
    data = bytes(buf)
    while i < len(data):
        if data[i] == 0x1B:
            # Escape sequence — take up to 6 bytes or until final byte.
            j = i + 1
            while j < len(data) and j - i < 8:
                b = data[j]
                j += 1
                if 0x40 <= b <= 0x7E and b not in (0x5B, 0x4F):  # final
                    break
            keys.append(data[i:j])
            i = j
        else:
            keys.append(data[i : i + 1])
            i += 1
    buf.clear()
    return keys


def forward_key(surface_id: str, key: bytes) -> None:
    name = KEY_MAP.get(key)
    if name:
        send_key(surface_id, name)
        return
    # Ctrl letters
    if len(key) == 1 and 1 <= key[0] <= 26 and key[0] not in (9, 10, 13):
        send_key(surface_id, f"ctrl+{chr(key[0] + 96)}")
        return
    try:
        text = key.decode("utf-8")
    except UnicodeDecodeError:
        return
    send_text(surface_id, text)


def session_attached(visibility_path: Path | None = None) -> bool:
    """Only the pane actually visible to an attached client owns the viewport.

    Session-attached is insufficient with sibling windows: every background
    mirror would otherwise resize and redraw its desktop surface on each swipe.
    Explicit pane targeting also survives moving windows between sessions.
    """
    pane = os.environ.get("TMUX_PANE")
    if not os.environ.get("TMUX"):
        return True
    if not pane:
        return False
    if visibility_path is not None:
        sample = load_state(visibility_path)
        if time.time() - sample.get("updated", 0) < 2:
            return pane in sample.get("panes", [])
    try:
        visible = subprocess.check_output(
            ["tmux", "list-clients", "-F", "#{pane_id}"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        ).splitlines()
        return pane in visible
    except (subprocess.SubprocessError, OSError):
        return False


def refresh_names(state: dict, state_path: Path) -> None:
    """Keep tmux session/window names aligned with the live cmux tab title."""
    if state.get("grouped"):
        return  # the topology reconciler owns shared session/window names
    surface_id = state.get("surface_id") or ""
    if not surface_id:
        return
    title_py = Path.home() / ".claude/hooks/moshi-cmux-title.py"
    try:
        title = subprocess.check_output(
            [sys.executable, str(title_py), "--title", surface_id],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip() or (state.get("title") or "cmux")
        desired = subprocess.check_output(
            [
                sys.executable,
                str(title_py),
                "--format-session",
                title,
                state.get("cwd") or "",
                "stale" if state.get("stale") is True else "active",
            ],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return

    current = subprocess.check_output(
        ["tmux", "display-message", "-p", "#{session_name}"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    if desired and current and desired != current:
        # Avoid clobbering an unrelated session that already owns the name.
        target = desired
        index = 2
        while target != current and subprocess.run(
            ["tmux", "has-session", "-t", f"{target}:"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            target = f"{desired}-{index}"
            index += 1
        renamed = subprocess.run(
            ["tmux", "rename-session", "-t", current, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if renamed:
            current = target
            log(f"mirror renamed session -> {current} title={title}")

    win = re.sub(r"[^\w\s\-]", "", title)[:40].strip() or current
    subprocess.run(
        ["tmux", "rename-window", "-t", f"{current}:0", win],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["tmux", "select-pane", "-t", f"{current}:0", "-T", title],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    state["title"] = title
    state["tmux_session"] = current
    try:
        state_path.write_text(json.dumps(state, indent=2) + "\n")
    except OSError:
        pass


def draw_idle_status(state: dict) -> None:
    title = state.get("title") or "cmux"
    surface = state.get("surface_id") or "?"
    sys.stdout.write(
        "\x1b[H\x1b[J"
        "Moshi ↔ cmux (idle)\r\n"
        "===================\r\n"
        f"tab:     {title}\r\n"
        f"surface: {surface}\r\n"
        "\r\n"
        "Live agent is in cmux on the Mac.\r\n"
        "Open this session from Moshi to mirror it.\r\n"
        "\r\n"
        "(idle — not polling cmux until a phone attaches)\r\n"
    )
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("state_file")
    ap.add_argument("queue_dir")
    ap.add_argument("--fps", type=float, default=8.0)
    args = ap.parse_args()

    state_path = Path(args.state_file)
    queue_dir = Path(args.queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)

    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        log("mirror: not a tty; refusing to run")
        return 1

    old = termios.tcgetattr(sys.stdin.fileno())
    stop = False

    def handle_sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGHUP, handle_sig)

    winch = {"flag": True}

    def on_winch(_signum, _frame):
        winch["flag"] = True

    signal.signal(signal.SIGWINCH, on_winch)

    tty.setraw(sys.stdin.fileno())
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt screen, hide cursor until drawn
    sys.stdout.flush()

    last_rev = None
    last_epoch = None
    last_seq = None
    last_name_refresh = 0.0
    last_idle_draw = 0.0
    was_attached = False
    viewport_surface = None
    cols = rows = 0
    key_buf = bytearray()
    active_interval = 1.0 / max(args.fps, 1.0)
    idle_interval = 2.0  # only drain hooks; do not hammer cmux

    try:
        while not stop:
            state = load_state(state_path)
            surface_id = state.get("surface_id") or ""
            if viewport_surface and viewport_surface != surface_id:
                clear_viewport(viewport_surface)
                viewport_surface = None
                last_rev = last_epoch = last_seq = None
            if not surface_id:
                sys.stdout.write(
                    "\x1b[H\x1b[JMoshi ↔ cmux mirror\r\n"
                    "Waiting for cmux surface binding…\r\n"
                )
                sys.stdout.flush()
                drain_queue(queue_dir)
                time.sleep(idle_interval)
                continue

            now = time.time()
            if now - last_name_refresh >= 120:
                refresh_names(state, state_path)
                state = load_state(state_path)
                last_name_refresh = now

            visibility_path = state_path.parent / "visibility.json" if state.get("grouped") else None
            attached = session_attached(visibility_path)

            # Phone disconnected: stop resizing/mirroring immediately so the
            # desktop cmux tab stops flashing.
            if was_attached and not attached:
                if viewport_surface:
                    clear_viewport(viewport_surface)
                    viewport_surface = None
                winch["flag"] = False
                last_rev = last_epoch = last_seq = None
                draw_idle_status(state)
                last_idle_draw = now
                log(f"mirror idle (detached) surface={surface_id}")

            if attached and not was_attached:
                winch["flag"] = True
                last_rev = last_epoch = last_seq = None
            was_attached = attached

            if not attached:
                # Idle: never call terminal.replay / terminal.viewport.
                if now - last_idle_draw >= 30:
                    draw_idle_status(state)
                    last_idle_draw = now
                drain_queue(queue_dir)
                # Discard any stray stdin so it does not buffer forever.
                while select.select([sys.stdin], [], [], 0)[0]:
                    chunk = os.read(sys.stdin.fileno(), 1024)
                    if not chunk:
                        stop = True
                        break
                time.sleep(.25 if visibility_path and visibility_path.exists() else idle_interval)
                continue

            local_cols, local_rows = term_size()
            if winch["flag"] or (local_cols, local_rows) != (cols, rows):
                cols, rows = local_cols, local_rows
                last_rev = last_epoch = last_seq = None
            winch["flag"] = False
            paint_cols, paint_rows = cols, rows

            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if not r:
                    break
                chunk = os.read(sys.stdin.fileno(), 1024)
                if not chunk:
                    stop = True
                    break
                key_buf.extend(chunk)
                for key in read_keys(key_buf):
                    forward_key(surface_id, key)

            viewport_surface = surface_id
            replay = replay_viewport(surface_id, paint_cols, paint_rows)
            if "_error" not in replay:
                grid = replay.get("render_grid") or {}
                cur_epoch = grid.get("render_epoch")
                cur_rev = grid.get("render_revision")
                cur_seq = grid.get("state_seq")
                if grid_fits(grid, paint_cols, paint_rows) and (cur_epoch, cur_rev, cur_seq) != (last_epoch, last_rev, last_seq):
                    frame = build_frame(grid, paint_cols, paint_rows)
                    sys.stdout.write(frame)
                    sys.stdout.flush()
                    last_epoch, last_rev, last_seq = cur_epoch, cur_rev, cur_seq

            drain_queue(queue_dir)
            time.sleep(active_interval)
    finally:
        if viewport_surface:
            clear_viewport(viewport_surface)
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        except termios.error:
            pass
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        log(f"mirror exit surface={load_state(state_path).get('surface_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
