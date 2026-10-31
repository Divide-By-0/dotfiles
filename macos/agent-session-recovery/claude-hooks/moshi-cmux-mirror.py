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
import math
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
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def term_size() -> tuple[int, int]:
    size = os.get_terminal_size(sys.stdout.fileno())
    # Keep a usable minimum so Claude/Codex TUIs can draw.
    cols = max(20, size.columns)
    rows = max(10, size.lines)
    return cols, rows


def clear_viewport(surface_id: str) -> bool:
    result = rpc(
        "terminal.viewport",
        {"surface_id": surface_id, "client_id": VIEWPORT_CLIENT_ID, "clear": True},
    )
    return "_error" not in result


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


class ViewportLease:
    """Own only this mirror's expiring report; retain failed clears for retry.

    Renew independently of painting: reading scrollback must not let cmux
    restore desktop width and then shrink it again on the next keystroke.
    Clearing our client report lets cmux calculate the *current* desktop size
    and respects any other client's report; never restore a saved old size.
    """
    REFRESH_SECONDS = 1.0
    RETRY_SECONDS = 1.0

    def __init__(self):
        self.surface = None
        self.pending = {}
        self.last_refresh = float('-inf')
        self.size = None

    def release(self):
        if self.surface:
            self.pending[self.surface] = float('-inf')
            self.surface = None
            self.size = None

    def renew(self, surface_id, cols, rows, now, *, paint=False):
        if self.surface != surface_id:
            self.release()
            self.surface = surface_id
            self.last_refresh = float('-inf')
        # Returning to a surface supersedes its pending clear. A delayed retry
        # must never remove the viewport we just reacquired.
        self.pending.pop(surface_id, None)
        if paint or self.size != (cols, rows) or now - self.last_refresh >= self.REFRESH_SECONDS:
            # Record ownership even on RPC failure: cmux may have applied the
            # request before the connection failed delivering the response.
            self.size = (cols, rows)
            self.last_refresh = now
            return replay_viewport(surface_id, cols, rows)
        return None

    def retry_cleanup(self, now, *, force=False):
        for surface_id, attempted_at in list(self.pending.items()):
            if force or now - attempted_at >= self.RETRY_SECONDS:
                self.pending[surface_id] = now
                if clear_viewport(surface_id):
                    self.pending.pop(surface_id, None)
                    log(f"mirror released desktop size surface={surface_id}")

    def close(self):
        self.release()
        # Normal exit gets bounded retries; an unresponsive server or SIGKILL
        # still falls back to cmux's expiring replay reports.
        for attempt in range(3):
            self.retry_cleanup(time.monotonic(), force=True)
            if not self.pending:
                break
            if attempt < 2:
                time.sleep(.1)


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
    # A clear exposes the phone's default background before styled cells arrive.
    # Overwrite every cell inside one synchronized update instead; use the same
    # native defaults for blank cells in live output and history.
    base = "\x1b[0m" + style_ansi({
        "foreground": grid.get("terminal_foreground"),
        "background": grid.get("terminal_background"),
    })
    out: list[str] = ["\x1b[?2026h\x1b[?25l" + base]
    for r in range(view_rows):
        current = None
        buf: list[str] = [f"\x1b[{r + 1};1H"]
        for c in range(view_cols):
            ch = lines[r][c]
            if ch == "":
                continue  # wide-glyph spacer
            a = line_styles[r][c]
            if a != current:
                if current:
                    buf.append(base)
                if a:
                    buf.append(a)
                current = a
            buf.append(ch)
        if current:
            buf.append(base)
        out.append("".join(buf))

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
    out.append("\x1b[?2026l")
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
    """Split raw stdin bytes into key sequences.

    CSI sequences are read to their final byte with no short length cap.
    REASON: an SGR mouse report (ESC [ < 64 ; 12 ; 30 M) is longer than the old
    8-byte cap, and the leftover "30M" was typed into the cmux terminal.
    """
    keys: list[bytes] = []
    i = 0
    data = bytes(buf)
    while i < len(data):
        if data[i] != 0x1B:
            keys.append(data[i : i + 1])
            i += 1
            continue
        nxt = data[i + 1] if i + 1 < len(data) else None
        if nxt == 0x5B and data[i + 2 : i + 3] == b"M" and len(data) >= i + 6:
            j = i + 6  # legacy X10 mouse report: ESC [ M + 3 raw bytes
        elif nxt == 0x5B:  # CSI: parameters/intermediates, then a final byte
            j = i + 2
            while j < len(data) and j - i < 64:
                j += 1
                if 0x40 <= data[j - 1] <= 0x7E:
                    break
        elif nxt == 0x4F and i + 2 < len(data):  # SS3, e.g. ESC O A
            j = i + 3
        else:
            j = i + 1  # bare ESC; Alt+key arrives as ESC then the key
        keys.append(data[i:j])
        i = j
    buf.clear()
    return keys


def mouse_event(key: bytes) -> str | None:
    """'up'/'down' for wheel reports, 'other' for any other mouse report."""
    if key.startswith(b"\x1b[M") and len(key) == 6:
        button = key[3] - 32
    elif key.startswith(b"\x1b[<") and key[-1:] in (b"M", b"m"):
        try:
            button = int(key[3:-1].split(b";")[0])
        except ValueError:
            return "other"
    else:
        return None
    if button & 64 and not button & 128:
        return "up" if button & 1 == 0 else "down"
    return "other"


SCROLL_STEP = 3  # lines per wheel notch, like most terminals


def scroll_window(lines: list[str], rows: int, offset: int) -> tuple[list[str], int]:
    """Lines to show `offset` lines above the bottom, keeping one status row."""
    body = max(1, rows - 1)
    offset = max(0, min(offset, len(lines) - body))
    end = len(lines) - offset
    return lines[max(0, end - body) : end], offset


def scrollback_frame(surface_id: str, cols: int, rows: int, offset: int) -> tuple[str, int]:
    body = max(1, rows - 1)
    replay = rpc("terminal.replay", {
        "surface_id": surface_id, "anchor": "screen",
        "max_scrollback_rows": body + offset,
    })
    if "_error" in replay:
        return "", offset  # Keep the last good frame/position on transient failure.
    grid = replay.get("render_grid") or {}
    if "scrollback_spans" in grid and "scrollback_rows" in grid:
        # Native history rows are numbered from zero, independently of screen
        # rows. Join their coordinate spaces before selecting the phone window.
        history = int(grid["scrollback_rows"])
        total = history + int(grid.get("rows") or rows)
        offset = max(0, min(offset, max(0, total - body)))
        start = max(0, total - offset - body)
        spans = []
        for source, shift in ((grid["scrollback_spans"], 0), (grid.get("row_spans") or [], history)):
            for span in source:
                row = int(span.get("row") or 0) + shift - start
                if 0 <= row < body:
                    spans.append(dict(span, row=row))
    else:
        # Older cmux versions only expose plain history. Preserve scrolling,
        # but current servers always use styled replay above.
        got = rpc("surface.read_text", {"surface_id": surface_id, "lines": body + offset})
        if "_error" in got:
            return "", offset
        lines = (got.get("text") or "").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        visible, offset = scroll_window(lines, rows, offset)
        spans = [{"row": n, "column": 0, "text": line} for n, line in enumerate(visible)]
    status = f" scrollback -{offset} lines · scroll down or type to return "[:cols]
    styles = list(grid.get("styles") or [])
    status_id = max((s.get("id", -1) for s in styles), default=-1) + 1
    styles.append({"id": status_id, "inverse": True})
    spans.append({"row": rows - 1, "column": 0, "text": status.ljust(cols), "style_id": status_id})
    frame = dict(grid, row_spans=spans, styles=styles, cursor={"visible": False})
    return build_frame(frame, cols, rows), offset


def forward_key(surface_id: str, key: bytes) -> None:
    name = KEY_MAP.get(key)
    if name:
        send_key(surface_id, name)
        return
    # Preserve control bytes, rather than synthesizing Ctrl-letter keys.
    # Named-key injection can lose these under the Kitty keyboard protocol.
    # Enter/Tab/Backspace were handled above and retain their key semantics.
    if len(key) == 1 and 1 <= key[0] <= 26:
        rpc("terminal.input", {"surface_id": surface_id, "text": key.decode("ascii")})
        return
    try:
        text = key.decode("utf-8")
    except UnicodeDecodeError:
        return
    send_text(surface_id, text)


# REASON: every desktop resize makes Claude Code reprint its whole conversation
# into the cmux scrollback. Releasing the phone width on every swipe stacked ~40
# phone-width copies in one tab (2026-09-28), which read as corrupted output
# when scrolling up in cmux. Hold the width across swipes; release only when
# the phone is gone or has sent no input for PHONE_IDLE_SECONDS.
PHONE_IDLE_SECONDS = 300  # Moshi in the background keeps its tmux client attached
HOLD_SECONDS = 300


def viewport_action(now: float, visible: bool, active: bool, last_visible: float | None) -> str:
    """'paint' this pane, 'hold' its phone-size viewport, or 'release' it."""
    if visible:
        return "paint"
    if active and last_visible is not None and now - last_visible < HOLD_SECONDS:
        return "hold"
    return "release"


def phone_state(visibility_path: Path | None = None) -> tuple[bool, bool]:
    """(this pane is on a phone screen, a phone sent input recently).

    Only the pane actually visible to an attached client owns the viewport.
    Session-attached is insufficient with sibling windows: every background
    mirror would otherwise resize and redraw its desktop surface on each swipe.
    Explicit pane targeting also survives moving windows between sessions.
    """
    pane = os.environ.get("TMUX_PANE")
    if not os.environ.get("TMUX"):
        return True, True
    if not pane:
        return False, False
    panes: list[str] | None = None
    activity = None
    if visibility_path is not None:
        sample = load_state(visibility_path)
        # The watcher owns grouped visibility. A slow/dead watcher must not
        # turn 96 idle mirrors into 96 tmux subprocesses every polling round.
        # Fail idle until a fresh sample arrives, including on first startup.
        updated = sample.get("updated")
        panes = sample.get("panes")
        activity = sample.get("activity")
        if (not isinstance(updated, (int, float)) or not math.isfinite(updated) or
                not 0 <= time.time() - updated < 2 or not isinstance(panes, list) or
                not all(isinstance(p, str) for p in panes) or
                (activity is not None and (not isinstance(activity, (int, float)) or not math.isfinite(activity)))):
            return False, False
    if panes is None:
        try:
            rows = subprocess.check_output(
                ["tmux", "list-clients", "-F", "#{pane_id} #{client_activity}"],
                text=True, timeout=3, stderr=subprocess.DEVNULL,
            ).split("\n")
        except (subprocess.SubprocessError, OSError):
            return False, False
        rows = [r.split() for r in rows if r.strip()]
        panes = [r[0] for r in rows]
        activity = max((int(r[1]) for r in rows if len(r) > 1 and r[1].isdigit()), default=0)
    if not panes:
        return False, False
    # Older samples carry no activity; treat any attached client as in use.
    active = activity is None or time.time() - float(activity) < PHONE_IDLE_SECONDS
    return active and pane in panes, active


def session_attached(visibility_path: Path | None = None) -> bool:
    return phone_state(visibility_path)[0]


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

    # TCSAFLUSH (setraw's default) loses keys queued while the launcher starts.
    tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    # REASON: mouse reporting (1000 + SGR 1006) makes tmux forward wheel events
    # to this process instead of opening copy-mode, which has no history on
    # this alternate-screen pane, so vertical swipes on the phone did nothing.
    # Wheel events now scroll the cmux tab's own scrollback (surface.read_text).
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")  # alt screen, hide cursor until drawn
    sys.stdout.flush()

    last_rev = None
    last_epoch = None
    last_seq = None
    last_name_refresh = 0.0
    last_idle_draw = 0.0
    was_attached = False
    viewport = ViewportLease()
    cols = rows = 0
    pending_size = None
    size_changed_at = 0.0
    key_buf = bytearray()
    last_visible: float | None = None
    scroll_offset = 0  # lines above live output; 0 means mirroring live
    scroll_dirty = False
    active_interval = 1.0 / max(args.fps, 1.0)
    idle_interval = 2.0  # only drain hooks; do not hammer cmux

    try:
        while not stop:
            now = time.monotonic()
            state = load_state(state_path)
            surface_id = state.get("surface_id") or ""
            if viewport.surface and viewport.surface != surface_id:
                viewport.release()
                last_rev = last_epoch = last_seq = None
                scroll_offset = 0
                scroll_dirty = False
            if not surface_id:
                viewport.retry_cleanup(now)
                sys.stdout.write(
                    "\x1b[H\x1b[JMoshi ↔ cmux mirror\r\n"
                    "Waiting for cmux surface binding…\r\n"
                )
                sys.stdout.flush()
                drain_queue(queue_dir)
                time.sleep(idle_interval)
                continue

            if now - last_name_refresh >= 120:
                refresh_names(state, state_path)
                state = load_state(state_path)
                last_name_refresh = now

            visibility_path = state_path.parent / "visibility.json" if state.get("grouped") else None
            attached, phone_active = phone_state(visibility_path)
            if attached:
                # Cancel a failed detach clear *before* retrying cleanup. The
                # phone may already be back on that same surface this tick.
                viewport.pending.pop(surface_id, None)
            viewport.retry_cleanup(now)
            if attached:
                last_visible = now
            action = viewport_action(now, attached, phone_active, last_visible)

            # Off the phone screen: stop painting. The desktop viewport is held
            # or released below, not dropped here (see viewport_action).
            if was_attached and not attached:
                winch["flag"] = False
                last_rev = last_epoch = last_seq = None
                draw_idle_status(state)
                last_idle_draw = now
                log(f"mirror idle (not visible) surface={surface_id}")

            if attached and not was_attached:
                winch["flag"] = True
                last_rev = last_epoch = last_seq = None
            was_attached = attached

            if not attached:
                if action == "hold" and viewport.surface:
                    # Replay reports expire unless refreshed; keep the phone size
                    # alive without painting so a swipe back needs no resize.
                    viewport.renew(viewport.surface, cols, rows, now)
                elif viewport.surface:
                    viewport.release()
                    viewport.retry_cleanup(now)
                if now - last_idle_draw >= 30:
                    draw_idle_status(state)
                    last_idle_draw = now
                drain_queue(queue_dir)
                # tmux only sends input to the selected pane. Keep it queued
                # until shared visibility catches up after a swipe; discarding
                # here loses the first key typed on a newly visible phone tab.
                delay = .25 if visibility_path and visibility_path.exists() else idle_interval
                if viewport.pending:
                    delay = min(delay, viewport.RETRY_SECONDS)
                time.sleep(delay)
                continue

            local_cols, local_rows = term_size()
            if pending_size != (local_cols, local_rows):
                pending_size = (local_cols, local_rows)
                size_changed_at = now
            # Initial attach is immediate; keyboard/rotation resize bursts settle
            # before SIGWINCH reaches the real agent and pollutes its history.
            if not cols or (pending_size != (cols, rows) and now - size_changed_at >= .15):
                cols, rows = local_cols, local_rows
                last_rev = last_epoch = last_seq = None
                scroll_dirty = bool(scroll_offset)
            if winch["flag"]:
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
                    event = mouse_event(key)
                    if event == "up":
                        scroll_offset += SCROLL_STEP
                        scroll_dirty = True
                    elif event == "down":
                        if scroll_offset:
                            scroll_offset = max(0, scroll_offset - SCROLL_STEP)
                            scroll_dirty = True
                            if not scroll_offset:
                                last_rev = last_epoch = last_seq = None
                    elif event is None:
                        if scroll_offset:  # typing returns to live output
                            scroll_offset = 0
                            last_rev = last_epoch = last_seq = None
                        forward_key(surface_id, key)
                    # other mouse events (taps, drags) are dropped, never typed

            if scroll_offset:
                replay = viewport.renew(surface_id, paint_cols, paint_rows, now, paint=scroll_dirty)
                # A resize can apply before cmux has a frame at the new size.
                # Keep history dirty and wait for reflow rather than painting
                # once from the old-width buffer and leaving it clipped.
                if (scroll_dirty and replay is not None and "_error" not in replay
                        and grid_fits(replay.get("render_grid") or {}, paint_cols, paint_rows)):
                    frame, scroll_offset = scrollback_frame(surface_id, paint_cols, paint_rows, scroll_offset)
                    if frame:
                        sys.stdout.write(frame)
                        sys.stdout.flush()
                        scroll_dirty = False
                    if not scroll_offset:
                        last_rev = last_epoch = last_seq = None
                drain_queue(queue_dir)
                time.sleep(active_interval)
                continue

            replay = viewport.renew(surface_id, paint_cols, paint_rows, now, paint=True)
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
        viewport.close()
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        except termios.error:
            pass
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        log(f"mirror exit surface={load_state(state_path).get('surface_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
