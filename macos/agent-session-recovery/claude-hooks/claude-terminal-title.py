#!/usr/bin/env python3
"""Give Claude terminal titles an unambiguous provider label.

Claude's own title generator describes the task, not the running provider. A
Codex handoff therefore becomes "Codex task <id> ..." even inside Claude.
This hook replaces native OSC titles; cmux's explicit tab names still win.
It never writes to stdout (which Claude treats as model context).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata


def clean(value: str) -> str:
    # OSC titles must never contain terminal commands, newlines or bidi marks.
    return " ".join("".join(c for c in value if not unicodedata.category(c).startswith("C")).split())[:120]


def title_for(state: dict) -> str:
    explicit = clean(str(state.get("customTitle") or ""))
    if explicit:
        return explicit
    topic = clean(str(state.get("aiTitle") or state.get("promptTitle") or ""))
    topic = re.sub(r"^codex\s+task\s+[0-9a-f]{8}(?:-[0-9a-f-]{27})?\b\s*[:—-]?\s*", "", topic, flags=re.I)
    if not topic:
        return "Claude"
    return topic[0].upper() + topic[1:] + " — Claude"


def read_titles(transcript: Path, session: str, cache_root: Path) -> dict:
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = cache_root / (hashlib.sha256(session.encode()).hexdigest() + ".json")
    with cache.open("a+", encoding="utf-8") as stored:
        # Hooks can overlap; the next event picks up any unread metadata.
        fcntl.flock(stored, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stored.seek(0)
        try:
            state = json.load(stored)
        except (ValueError, TypeError):
            state = {}
        with transcript.open("rb") as source:
            stat = os.fstat(source.fileno())
            identity = [stat.st_dev, stat.st_ino]
            offset = state.get("offset", 0)
            if state.get("file") != identity or not isinstance(offset, int) or offset > stat.st_size:
                state = {}
                offset = 0
            source.seek(offset)
            while True:
                start = source.tell()
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    source.seek(start)
                    break
                # Title metadata for resumed sessions; the first human prompt
                # supplies a local label when native title generation is disabled.
                if b'"ai-title"' not in line and b'"custom-title"' not in line and (state.get("promptTitle") or b'"user"' not in line or b'"toolUseResult"' in line):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("sessionId") != session:
                    continue
                if record.get("type") == "custom-title":
                    state["customTitle"] = record.get("customTitle", "")
                elif record.get("type") == "ai-title":
                    state["aiTitle"] = record.get("aiTitle", "")
                elif record.get("type") == "user" and record.get("origin", {}).get("kind") in (None, "human"):
                    content = record.get("message", {}).get("content")
                    if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                        state["promptTitle"] = clean(content.splitlines()[0])[:80]
            state.update(file=identity, offset=source.tell())
        stored.seek(0)
        json.dump(state, stored)
        stored.truncate()
        os.chmod(cache, 0o600)
        return state


def terminal_fd() -> int:
    try:
        return os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        pass
    # Claude starts hooks without a controlling tty. Resolve the inherited cmux
    # surface explicitly; focus/current-workspace defaults would target other tabs.
    sid = os.environ.get("CMUX_SURFACE_ID", "")
    workspace = os.environ.get("CMUX_WORKSPACE_ID", "")
    tty = ""
    if sid and workspace:
        cli = os.environ.get("CMUX_BUNDLED_CLI_PATH") or "cmux"
        raw = subprocess.check_output([cli, "rpc", "debug.terminals", "{}"], timeout=1, stderr=subprocess.DEVNULL)
        for terminal in json.loads(raw).get("terminals", []):
            if str(terminal.get("surface_id", "")).lower() == sid.lower() and str(terminal.get("last_known_workspace_id", "")).lower() == workspace.lower():
                tty = terminal.get("tty") or ""
                break
    elif not sid:
        # Standalone Claude hooks are detached too. Only the actual Claude
        # ancestor's tty is eligible, never an arbitrary ancestor shell.
        pid = os.getppid()
        for _ in range(5):
            raw = subprocess.check_output(["ps", "-p", str(pid), "-o", "ppid=,tty=,comm="], timeout=1, text=True)
            parent, candidate, command = raw.strip().split(maxsplit=2)
            if Path(command).name == "claude":
                tty = candidate
                break
            pid = int(parent)
            if pid <= 1:
                break
    if not re.fullmatch(r"(?:ttys?\d+|pts/\d+)", tty):
        raise OSError("no matching agent terminal")
    return os.open("/dev/" + tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        session = payload.get("session_id")
        transcript = payload.get("transcript_path")
        if not session or not transcript or payload.get("agent_id"):
            return 0
        terminal = terminal_fd()
        try:
            root = Path.home() / ".cache/claude-terminal-titles"
            try:
                state = read_titles(Path(transcript), session, root)
            except FileNotFoundError:
                state = {}
            sequence = ("\x1b]2;" + title_for(state) + "\x07").encode()
            os.write(terminal, sequence)
        finally:
            os.close(terminal)
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        # Title bookkeeping must never block or fail an agent tool call.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
