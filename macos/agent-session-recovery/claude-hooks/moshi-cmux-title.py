#!/usr/bin/env python3
"""Resolve human cmux titles and stable, readable Moshi tmux names."""
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


def _slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"^[^\w]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[^\w\s\-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    return text


def format_session_name(title: str, cwd: str, stale: bool = False) -> str:
    """Build a tmux-safe name without leaking opaque cmux surface IDs.

    The title says what the agent is doing and the directory basename says where.
    Collisions are resolved by the caller with numeric suffixes.
    """
    title_slug = _slug(title)
    cwd_slug = _slug(_basename(cwd)) if cwd else ""
    generic = {
        "",
        "cmux",
        "tab",
        "terminal",
        "untitled",
    }
    if title_slug in generic or re.fullmatch(r"[0-9a-f]{8,}", title_slug):
        title_slug = "claude"
    if cwd_slug in {"", ".", "claude"} or cwd_slug == title_slug:
        descriptive = title_slug
    else:
        descriptive = f"{title_slug}-{cwd_slug}"
    prefix = "stale-moshi" if stale else "moshi"
    return f"{prefix}-{descriptive}"[:63].rstrip("-")


def main() -> int:
    mode = "title"
    args = sys.argv[1:]
    if args and args[0] == "--format-session":
        title = args[1] if len(args) > 1 else ""
        cwd = args[2] if len(args) > 2 else ""
        state = args[3] if len(args) > 3 else "active"
        print(format_session_name(title, cwd, stale=(state == "stale")))
        return 0
    if args and args[0] in {"--session-name", "--title"}:
        mode = "session" if args[0] == "--session-name" else "title"
        args = args[1:]
    sid = (args[0] if args else "").strip()
    cwd = (args[1] if len(args) > 1 else "").strip()
    previous = ""
    # Optional: previous title from state for better fallback ranking.
    if len(args) > 2:
        previous = args[2]
    title = resolve_title(sid, previous=previous)
    if mode == "session":
        print(format_session_name(title, cwd))
    else:
        print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
