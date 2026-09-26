#!/usr/bin/env python3
"""Rename Moshi-only tmux helpers to readable active/stale names."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=check,
    )


def tmux(tmux_bin: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    command = [tmux_bin]
    socket = os.environ.get("TMUX_SOCKET")
    if socket:
        command.extend(["-L", socket])
    return run([*command, *args], check=check)


def load_formatter(title_py: Path):
    spec = importlib.util.spec_from_file_location("moshi_cmux_title", title_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load title formatter: {title_py}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module.format_session_name


def process_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    result = run(["ps", "-axo", "pid=,ppid=,command="])
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if not match:
            continue
        pid, ppid = int(match.group(1)), int(match.group(2))
        commands[pid] = match.group(3)
        children.setdefault(ppid, []).append(pid)
    return children, commands


def descendants(root: int, children: dict[int, list[int]]) -> set[int]:
    found: set[int] = set()
    pending = [root]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def has_live_mirror(
    tmux_bin: str,
    session: str,
    children: dict[int, list[int]],
    commands: dict[int, str],
) -> bool:
    panes = tmux(
        tmux_bin,
        "list-panes",
        "-t",
        f"{session}:",
        "-F",
        "#{pane_pid}\t#{pane_dead}\t#{pane_start_command}",
    )
    for raw in panes.stdout.splitlines():
        fields = raw.split("\t", 2)
        try:
            pane_pid = int(fields[0].strip())
        except ValueError:
            continue
        pane_dead = len(fields) > 1 and fields[1] == "1"
        start_command = fields[2] if len(fields) > 2 else ""
        # A just-respawned keeper may still be its small start script when this
        # check runs. Restored stale helpers either start a shell or are dead.
        if not pane_dead and re.search(
            r"moshi-cmux-binds/[^/ ]+[.]start[.]sh", start_command
        ):
            return True
        for pid in {pane_pid, *descendants(pane_pid, children)}:
            command = commands.get(pid, "")
            if "moshi-cmux-mirror.py" in command or "moshi-ghost-keeper.sh" in command:
                return True
    return False


def state_matches(session: str, state: dict) -> bool:
    saved = str(state.get("tmux_session") or "")
    if saved and session == saved:
        return True

    surface = re.sub(r"[^a-f0-9]", "", str(state.get("surface_id") or "").lower())
    short, key = surface[:6], surface[:12]
    legacy = {f"moshi-cmux-{key}"} if key else set()
    if short:
        legacy.update({f"{short}-{short}", f"{short}-{short}-x"})
        if re.search(rf"-{re.escape(short)}(?:-x)?$", session):
            return True
    if key and session.endswith(f"-{key}"):
        return True
    if session in legacy:
        return True

    return False


def unique_name(base: str, current: str, occupied: set[str]) -> str:
    available = set(occupied)
    available.discard(current)
    if base not in available:
        return base
    index = 2
    while f"{base}-{index}" in available:
        index += 1
    return f"{base}-{index}"


def write_state(path: Path, state: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    tmux_bin = os.environ.get("TMUX_BIN") or "/opt/homebrew/bin/tmux"
    state_dir = Path(
        os.environ.get("MOSHI_STATE_DIR")
        or Path.home() / ".claude/hooks/moshi-cmux-binds"
    )
    title_py = Path(
        os.environ.get("MOSHI_TITLE_PY")
        # install.sh copies (not symlinks) runtime files out of ~/Documents, so the
        # formatter is found at its installed path, not relative to this file.
        or Path.home() / ".claude/hooks/moshi-cmux-title.py"
    )
    formatter = load_formatter(title_py)

    listed = tmux(tmux_bin, "list-sessions", "-F", "#{session_name}")
    if listed.returncode != 0:
        return 0
    sessions = sorted(filter(None, listed.stdout.splitlines()))
    occupied = set(sessions)
    children, commands = process_table()
    claimed: set[str] = set()
    changes = 0

    for state_path in sorted(state_dir.glob("*.json")):
        if state_path.name.endswith((".payload.json", ".rebind.json")):
            continue
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        original_saved = str(state.get("tmux_session") or "")
        matches = [
            session
            for session in sessions
            if session not in claimed and state_matches(session, state)
        ]
        matches.sort(key=lambda session: (session != original_saved, session))

        for session in matches:
            claimed.add(session)
            explicitly_stale = state.get("stale") is True
            stale = explicitly_stale or not has_live_mirror(
                tmux_bin, session, children, commands
            )
            try:
                base = formatter(
                    str(state.get("title") or ""),
                    str(state.get("cwd") or ""),
                    stale=stale,
                )
            except (OSError, RuntimeError):
                continue
            desired = unique_name(base, session, occupied)
            metadata_owner = session == original_saved

            if desired != session:
                if not args.dry_run:
                    renamed = tmux(
                        tmux_bin,
                        "rename-session",
                        "-t",
                        f"{session}:",
                        desired,
                    )
                    if renamed.returncode != 0:
                        continue
                occupied.discard(session)
                occupied.add(desired)
                changes += 1
                if not args.quiet:
                    print(f"{session} -> {desired}")

            if metadata_owner and not args.dry_run:
                state["tmux_session"] = desired
                state["stale"] = stale
                write_state(state_path, state)

    if not args.quiet:
        verb = "would rename" if args.dry_run else "renamed"
        print(f"moshi reconciliation: {verb} {changes} session(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
