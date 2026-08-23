#!/usr/bin/env python3
"""Resolve a human cmux surface title, and sanitize it for tmux session names."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata


def _rpc(method: str, params: dict | None = None) -> dict:
    args = ["cmux", "rpc", method]
    if params is not None:
        args.append(json.dumps(params))
    try:
        raw = subprocess.check_output(
            args, text=True, timeout=3, stderr=subprocess.DEVNULL
        )
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _looks_like_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("~")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def resolve_title(surface_id: str, previous: str = "") -> str:
    sid = (surface_id or "").strip()
    if not sid:
        return previous or "cmux"

    surface_titles: list[str] = []
    workspace_titles: list[str] = []

    data = _rpc("surface.list", {})
    for surface in data.get("surfaces") or []:
        if (surface.get("id") or "").lower() != sid.lower():
            continue
        title = (surface.get("title") or "").strip()
        if title:
            surface_titles.append(title)
        break

    data = _rpc("debug.terminals", {})
    for term in data.get("terminals") or []:
        if (term.get("surface_id") or "").lower() != sid.lower():
            continue
        st = (term.get("surface_title") or "").strip()
        wt = (term.get("workspace_title") or "").strip()
        if st:
            surface_titles.append(st)
        if wt:
            workspace_titles.append(wt)
        break

    def normalize(value: str) -> str:
        value = value.strip()
        if _looks_like_path(value):
            value = _basename(value)
        return value[:80]

    def usable(value: str) -> bool:
        if not value:
            return False
        if re.fullmatch(r"[0-9A-Fa-f]{8,}", value):
            return False
        return True

    for raw in surface_titles:
        value = normalize(raw)
        if usable(value):
            return value

    prev = normalize(previous) if previous else ""
    if usable(prev) and prev.lower() not in {"normal", "cmux", "tab", "terminal"}:
        return prev

    for raw in workspace_titles:
        value = normalize(raw)
        if usable(value):
            return value

    if prev:
        return prev
    return sid[:8] or "cmux"


def sanitize_session_name(title: str, surf_key: str) -> str:
    """tmux-safe session name derived from the tab title.

    Keeps a short surface suffix so renames stay unique across similarly named tabs.
    """
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Drop leading status glyphs / punctuation.
    text = re.sub(r"^[^\w]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    if not text or text in {"cmux", "terminal", "untitled"}:
        text = "tab"
    text = text[:36].rstrip("-")
    suffix = re.sub(r"[^a-f0-9]", "", (surf_key or "").lower())[:6]
    if suffix:
        return f"{text}-{suffix}"
    return text


def main() -> int:
    mode = "title"
    args = sys.argv[1:]
    if args and args[0] in {"--session-name", "--title"}:
        mode = "session" if args[0] == "--session-name" else "title"
        args = args[1:]
    sid = (args[0] if args else "").strip()
    surf_key = (args[1] if len(args) > 1 else "").strip()
    previous = ""
    # Optional: previous title from state for better fallback ranking.
    if len(args) > 2:
        previous = args[2]
    title = resolve_title(sid, previous=previous)
    if mode == "session":
        if not surf_key:
            surf_key = re.sub(r"[^a-f0-9]", "", sid.lower())[:12]
        print(sanitize_session_name(title, surf_key))
    else:
        print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
