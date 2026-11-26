#!/usr/bin/env python3
"""Read-only cmux checkpoints and explicit, duplicate-safe native session recovery.

Never infer a conversation from its title, cwd, or the newest transcript. Never
execute a command stored in a snapshot: build native argv from verified IDs.
"""
import argparse
import ctypes
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import struct
import sys
import tempfile
import time

UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
APP = "/Applications/cmux.app/Contents/MacOS/cmux"
RESOURCE_BIN = Path("/Applications/cmux.app/Contents/Resources/bin")
MAX_SNAPSHOT = 32 * 1024 * 1024


def load_json(path):
    with Path(path).open() as stream:
        return json.load(stream)


def atomic_json(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(argv, timeout=15):
    result = subprocess.run(argv, capture_output=True, text=True,
                            errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError("%s failed (%s): %s" % (
            argv[0], result.returncode, result.stderr.strip()[:300]))
    return result.stdout


def provider_for(executable):
    if executable.endswith("/codex") or executable == "codex":
        return "codex"
    if (executable.endswith("/claude") or executable == "claude"
            or "/claude/versions/" in executable):
        return "claude"
    return None


def argv_identity(provider, words):
    # Use kernel argv boundaries: a prompt containing "resume <uuid>" is not
    # a native resume command. ps's human-readable args cannot prove this.
    flags = {"--resume", "--session-id"} if provider == "claude" else {"resume"}
    ids = set()
    for index, word in enumerate(words):
        if word in flags and index + 1 < len(words) and UUID.fullmatch(words[index + 1]):
            ids.add(words[index + 1].lower())
        for flag in flags:
            if word.startswith(flag + "=") and UUID.fullmatch(word[len(flag) + 1:]):
                ids.add(word[len(flag) + 1:].lower())
    return ids


def native_arguments(pid):
    """Return argv and only the cmux routing UUID; discard all other environment."""
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
            size = ctypes.c_size_t()
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or size.value > 2 * 1024 * 1024:
                return [], None
            buffer = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
                return [], None
            raw = buffer.raw[:size.value]
            argc = struct.unpack_from("i", raw)[0]
            if not 0 < argc < 65536:
                return [], None
            pos = raw.index(b"\0", 4) + 1  # skip executable path and padding
            while pos < len(raw) and raw[pos] == 0:
                pos += 1
            argv = []
            for _ in range(argc):
                end = raw.index(b"\0", pos)
                argv.append(os.fsdecode(raw[pos:end]))
                pos = end + 1
            environment = raw[pos:].split(b"\0")
        else:
            root = Path("/proc") / str(pid)
            argv = [os.fsdecode(v) for v in (root / "cmdline").read_bytes().split(b"\0") if v]
            environment = (root / "environ").read_bytes().split(b"\0")
        for value in environment:
            if value.startswith(b"CMUX_SURFACE_ID="):
                candidate = value.split(b"=", 1)[1].decode("ascii", "ignore")
                if UUID.fullmatch(candidate):
                    return argv, candidate
        return argv, None
    except (OSError, ValueError, struct.error):
        return [], None


def process_table():
    result = {}
    for line in run(["/bin/ps", "-axo", "pid=,ppid=,tty=,lstart=,comm="]).splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9:
            continue
        try:
            started = time.mktime(time.strptime(" ".join(parts[3:8]), "%a %b %d %H:%M:%S %Y"))
            pid = int(parts[0])
        except ValueError:
            continue
        result[pid] = dict(pid=pid, ppid=int(parts[1]), tty=parts[2],
                           started=started, executable=parts[8])
    agents = [p for p in result.values() if provider_for(p["executable"]) and p["tty"] != "??"]
    for process in agents:
        # secretty gives the agent a different PTY from cmux's outer shell.
        # Read only the stable routing UUID, never persist the environment.
        process["argv"], surface_id = native_arguments(process["pid"])
        if surface_id:
            process["surface_id"] = surface_id
    return result


def snapshot_panels(snapshot):
    rows = []
    for window in snapshot.get("windows", []):
        for ws in window.get("tabManager", {}).get("workspaces", []):
            for panel in ws.get("panels", []):
                if panel.get("type") == "terminal" or "terminal" in panel:
                    rows.append(dict(panel, workspace_id=ws.get("workspaceId"),
                                     workspace_title=ws.get("customTitle") or ws.get("processTitle")))
    return rows


def tree_surfaces(tree):
    rows = []
    if not isinstance(tree.get("windows"), list):
        raise ValueError("cmux returned no valid window inventory")
    for window in tree["windows"]:
        for ws in window.get("workspaces", []):
            for pane in ws.get("panes", []):
                for surface in pane.get("surfaces", []):
                    if surface.get("type") == "terminal":
                        rows.append(dict(surface, workspace_id=ws.get("id"),
                                         workspace_title=ws.get("title")))
    return rows


def transcript_path(home, provider, session_id, hint=None):
    """Require local native history bearing this exact ID, not just a filename."""
    roots = [home / ".codex/sessions"] if provider == "codex" else [home / ".claude/projects"]
    candidates = [Path(hint)] if hint else []
    pattern = "????/??/??/*%s.jsonl" % session_id if provider == "codex" else "*/%s.jsonl" % session_id
    for root in roots:
        candidates.extend(root.glob(pattern))
    for path in dict.fromkeys(candidates):
        try:
            resolved = path.resolve()
            if not any(root.resolve() in resolved.parents for root in roots):
                continue
            with resolved.open() as stream:
                # Native files start with their session identity. Bound parsing
                # rather than scanning every conversation on every timer tick.
                for _ in range(100):
                    line = stream.readline(262145)
                    if not line or len(line) > 262144:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    found = (record.get("payload", {}).get("id")
                             if record.get("type") == "session_meta" else record.get("sessionId"))
                    if isinstance(found, str) and found.lower() == session_id.lower():
                        return str(resolved)
        except (OSError, UnicodeError):
            continue
    return None


def inventory(home, snapshot, tree, processes, hooks):
    surfaces = tree_surfaces(tree)
    saved = snapshot_panels(snapshot)
    sessions, unknown, observed = [], [], set()
    for process in processes.values():
        provider = provider_for(process["executable"])
        if not provider or "argv" not in process:
            continue
        # app-server/exec workers are not interactive agent conversations.
        if any(word in process["argv"][1:2] for word in ("app-server", "exec", "mcp-server")):
            continue
        direct = argv_identity(provider, process["argv"])
        matched_hooks = [h for h in hooks.get(provider, [])
                         if h.get("pid") == process["pid"]
                         and isinstance(h.get("pidStartSeconds"), (int, float))
                         and abs(h["pidStartSeconds"] - process["started"]) <= 1.1
                         and UUID.fullmatch(h.get("sessionId", ""))]
        hooked = {h["sessionId"].lower() for h in matched_hooks}
        ids = direct or hooked
        # A concrete resume argv beats an obsolete hook on that same process.
        for sid in ids:
            observed.add((provider, sid))
        route_ids = {h.get("surfaceId", "").lower() for h in matched_hooks}
        if process.get("surface_id"):
            route_ids.add(process["surface_id"].lower())
        # A live PID/start-time hook is stronger than inherited routing after a
        # tab move. TTY alone is insufficient: nested/reused PTYs can misroute.
        hook_routes = {h.get("surfaceId", "").lower() for h in matched_hooks}
        route_ids = hook_routes or route_ids
        matches = [s for s in surfaces if s.get("id", "").lower() in route_ids]
        if not matches:
            if route_ids:
                unknown.append(dict(provider=provider, pid=process["pid"],
                                    reason="live agent surface is absent from current cmux inventory"))
            continue
        surface = matches[0]
        label = dict(title=surface.get("title"), workspace_title=surface.get("workspace_title"),
                     workspace_id=surface.get("workspace_id"), surface_ref=surface.get("ref"),
                     surface_id=surface.get("id"), tty=process["tty"], provider=provider, pid=process["pid"])
        if len(matches) != 1 or len(ids) != 1:
            unknown.append(dict(label, reason="ambiguous identity" if ids else "native session ID not captured"))
            continue
        sid = next(iter(ids))
        hook = next((h for h in matched_hooks if h["sessionId"].lower() == sid), {})
        panel = next((p for p in saved if p.get("id", "").lower() == surface.get("id", "").lower()), {})
        terminal = panel.get("terminal", {})
        binding = terminal.get("resumeBinding") or {}
        cwd = hook.get("cwd") or terminal.get("workingDirectory") or panel.get("directory")
        if not cwd or not Path(cwd).is_absolute():
            unknown.append(dict(label, reason="working directory not captured"))
            continue
        history = transcript_path(home, provider, sid, hook.get("transcriptPath"))
        launch_args = (hook.get("launchCommand") or {}).get("arguments", [])
        bypass = ("--yolo" in process["argv"] or "--yolo" in launch_args
                  if provider == "codex" else
                  "--dangerously-skip-permissions" in process["argv"]
                  or hook.get("permissionMode") == "bypassPermissions")
        sessions.append(dict(label, session_id=sid, cwd=cwd, transcript_path=history,
                             history_available=history is not None, bypass_permissions=bypass,
                             identity_source="native-argv" if direct else "live-hook-pid-and-start-time",
                             saved_auto_resume=binding.get("autoResume"),
                             surface_id=panel.get("id") or hook.get("surfaceId")))
    # Keep disabled/exited checkpoints visible, but they are not evidence that
    # the user wanted that conversation running at the checkpoint.
    dormant = []
    for panel in saved:
        terminal = panel.get("terminal", {})
        binding = terminal.get("resumeBinding") or {}
        agent = terminal.get("agent") or {}
        provider = binding.get("kind") or agent.get("kind")
        sid = binding.get("checkpointId") or agent.get("sessionId")
        if provider in ("claude", "codex") and isinstance(sid, str) and UUID.fullmatch(sid):
            if (provider, sid.lower()) not in observed:
                dormant.append(dict(provider=provider, session_id=sid.lower(), title=panel.get("title"),
                                    workspace_title=panel.get("workspace_title"),
                                    cwd=binding.get("cwd") or agent.get("workingDirectory")
                                    or terminal.get("workingDirectory") or panel.get("directory"),
                                    transcript_path=None, bypass_permissions=False,
                                    saved_auto_resume=binding.get("autoResume")))
    return dict(sessions=sessions, unknown=unknown, dormant=dormant,
                terminal_count=len(surfaces)), observed


class Checkpoints:
    def __init__(self, home=None, state=None, cmux=None):
        self.home = Path(home or Path.home())
        self.state = Path(state or self.home / ".local/state/cmux-checkpoints")
        self.cmux = str(cmux or RESOURCE_BIN / "cmux")
        self.history = self.state / "checkpoints"

    def cli(self, *args):
        return json.loads(run([self.cmux, "--json", "--id-format", "both"] + list(args)))

    def collect(self):
        path = self.home / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
        if path.stat().st_size > MAX_SNAPSHOT:
            raise ValueError("snapshot exceeds the 32 MiB capture limit")
        raw = path.read_bytes()
        if len(raw) > MAX_SNAPSHOT:
            raise ValueError("snapshot exceeds the 32 MiB capture limit")
        snapshot = json.loads(raw)
        if not isinstance(snapshot.get("windows"), list):
            raise ValueError("invalid saved cmux snapshot")
        tree = self.cli("tree", "--all")  # API failure must not replace a good checkpoint.
        processes = process_table()
        hooks = {}
        for provider in ("claude", "codex"):
            try:
                hooks[provider] = list(load_json(self.home / ".cmuxterm" /
                                                (provider + "-hook-sessions.json")).get("sessions", {}).values())
            except (OSError, ValueError):
                hooks[provider] = []
        data, observed = inventory(self.home, snapshot, tree, processes, hooks)
        app = sorted([p["pid"], p["started"]] for p in processes.values() if p["executable"] == APP)
        data.update(schema=1, captured_at=time.time(), app_generation=app,
                    snapshot_sha256=hashlib.sha256(raw).hexdigest())
        return raw, data, observed

    def entries(self):
        result = []
        for path in sorted(self.history.glob("*/manifest.json"), reverse=True):
            try:
                result.append((path.parent, load_json(path)))
            except (OSError, ValueError):
                continue
        return result

    def prune(self, now, max_bytes=256 * 1024 * 1024):
        # Last 12h at timer resolution, one per hour for 7d, one per day for 30d.
        buckets, used = set(), 0
        for index, (path, manifest) in enumerate(self.entries()):
            age = now - manifest["captured_at"]
            stamp = dt.datetime.fromtimestamp(manifest["captured_at"], dt.timezone.utc)
            bucket = ("recent", path.name) if age < 12 * 3600 else (
                ("hour", stamp.strftime("%Y%m%d%H")) if age < 7 * 86400 else ("day", stamp.strftime("%Y%m%d")))
            size = sum(p.stat().st_size for p in path.iterdir() if p.is_file())
            if index == 0 or (age < 30 * 86400 and bucket not in buckets and used + size <= max_bytes):
                buckets.add(bucket)
                used += size
            else:
                shutil.rmtree(path)

    def capture(self):
        for name in ("launchd.out", "launchd.err"):
            log = self.state / name
            if log.exists() and log.stat().st_size > 1024 * 1024:
                os.replace(log, log.with_name(name + ".previous"))
        raw, data, _ = self.collect()
        # Retain read-only screen evidence for unidentified agents. This is not
        # used as an executable command or treated as a guessed conversation ID.
        for item in data["unknown"]:
            if item.get("surface_ref"):
                try:
                    screen = self.cli("read-screen", "--surface", item["surface_ref"], "--lines", "150")
                    item["screen_excerpt"] = screen.get("text", "")[-32000:]
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    item["screen_unavailable"] = True
        name = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        temp = Path(tempfile.mkdtemp(prefix=".capture-", dir=str(self.state)))
        try:
            with (temp / "layout.json.gz").open("wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
                    compressed.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_json(temp / "manifest.json", data)
            os.replace(temp, self.history / name)
            directory_fd = os.open(str(self.history), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        self.prune(data["captured_at"])
        status = dict(ok=True, checkpoint=name, captured_at=data["captured_at"],
                      sessions=len(data["sessions"]), unknown=len(data["unknown"]),
                      dormant=len(data["dormant"]), terminals=data["terminal_count"],
                      histories_missing=sum(not s["history_available"] for s in data["sessions"]))
        atomic_json(self.state / "status.json", status)
        return status

    def baseline(self, current, selected=None):
        entries = self.entries()
        if selected:
            if Path(selected).name != selected:
                raise ValueError("use a checkpoint name from cmux-checkpoint list")
            return next((e for e in entries if e[0].name == selected), (None, None))
        # After restart, prefer the last checkpoint of the previous app run.
        # Otherwise partially reopened sessions would replace the recovery set.
        prior = [e for e in entries if e[1]["sessions"]
                 and e[1].get("app_generation") != current.get("app_generation")]
        return next(iter(prior or [e for e in entries if e[1]["sessions"]]), (None, None))

    def recovery(self, selected=None, apply=False, only=None):
        _, current, observed = self.collect()
        path, checkpoint = self.baseline(current, selected)
        if not checkpoint:
            raise ValueError("no saved live-session checkpoint; run capture first")
        plan = []
        seen = set()
        # An exited checkpoint is offered only when the user selects its exact
        # ID; ordinary bulk recovery must not revive intentionally closed work.
        candidates = checkpoint["sessions"] + (checkpoint["dormant"] if only else [])
        reservations = self.state / "pending-launches.json"
        try:
            pending = load_json(reservations)
        except FileNotFoundError:
            pending = {}
        for saved in candidates:
            key = (saved["provider"], saved["session_id"])
            if key in seen or (only and saved["session_id"] != only.lower()):
                continue
            seen.add(key)
            item = dict(saved)
            if key in observed:
                item["action"] = "already-running"
            elif saved.get("surface_id") and any(
                    u.get("surface_id") == saved["surface_id"] for u in current["unknown"]):
                item["action"] = "identity-unconfirmed-in-occupied-tab"
            elif time.time() - pending.get(":".join(key), {}).get("time", 0) < 600:
                item["action"] = "launch-pending"
            elif not transcript_path(self.home, *key, saved.get("transcript_path")):
                item["action"] = "history-missing"
            elif not saved.get("cwd") or not Path(saved["cwd"]).is_dir():
                item["action"] = "directory-missing"
            else:
                item["action"] = "resume-in-new-tab"
                item["command"] = self.resume_command(saved)
            plan.append(item)
        if apply:
            workspace = None
            for item in plan:
                if item["action"] != "resume-in-new-tab":
                    continue
                # Recheck immediately before mutation: another client may have
                # resumed this exact conversation since the preview.
                _, latest, live = self.collect()
                if (item["provider"], item["session_id"]) in live:
                    item["action"] = "already-running"
                    continue
                if item.get("surface_id") and any(
                        u.get("surface_id") == item["surface_id"] for u in latest["unknown"]):
                    item["action"] = "identity-unconfirmed-in-occupied-tab"
                    continue
                # Commit the intent before the RPC. A lost response or slow
                # startup must not launch a second copy on an immediate retry.
                pending[":".join((item["provider"], item["session_id"]))] = dict(time=time.time())
                atomic_json(reservations, pending)
                if workspace is None:
                    # Legacy new-workspace prints "OK ..." even with --json.
                    # v2 RPC keeps the creation receipt structured across CLI versions.
                    reply = self.cli("rpc", "workspace.create", json.dumps(dict(
                        title="Recovered sessions " + path.name[:8], cwd=item["cwd"],
                        initial_input=item["command"] + "\r", focus=False)))
                    workspace = reply.get("workspace_id") or reply.get("workspace_ref")
                    if not workspace:
                        raise RuntimeError("created workspace without an identifier; stopped to avoid duplicate launches")
                else:
                    self.cli("rpc", "surface.create", json.dumps(dict(
                        workspace_id=workspace, type="terminal", working_directory=item["cwd"],
                        initial_input=item["command"] + "\r", focus=False)))
                item["action"] = "launch-requested"
        return dict(checkpoint=path.name, sessions=plan,
                    unknown=[{k: v for k, v in item.items() if k != "screen_excerpt"}
                             for item in checkpoint["unknown"]],
                    note="Native provider histories are required; layout is archived separately. No live process memory is saved.")

    def resume_command(self, saved):
        provider, sid = saved["provider"], saved["session_id"]
        if provider not in ("claude", "codex") or not UUID.fullmatch(sid):
            raise ValueError("invalid native session identity")
        wrapper = RESOURCE_BIN / ("cmux-" + provider + "-wrapper")
        executable = str(wrapper) if wrapper.is_file() else shutil.which(provider)
        if not executable:
            raise ValueError(provider + " executable is unavailable")
        argv = [executable, "resume", sid, "--no-daemon", "-c", "tui.resume_cwd=current"] if provider == "codex" else [executable, "--resume", sid]
        if saved.get("bypass_permissions"):
            argv.append("--yolo" if provider == "codex" else "--dangerously-skip-permissions")
        return "cd -- %s && %s" % (shlex.quote(saved["cwd"]), shlex.join(argv))

    def export_layout(self, checkpoint, output):
        if not checkpoint or Path(checkpoint).name != checkpoint or not output:
            raise ValueError("export-layout requires --checkpoint NAME and --output PATH")
        folder = self.history / checkpoint
        manifest = load_json(folder / "manifest.json")
        with gzip.open(folder / "layout.json.gz", "rb") as stream:
            raw = stream.read(MAX_SNAPSHOT + 1)
        if len(raw) > MAX_SNAPSHOT or hashlib.sha256(raw).hexdigest() != manifest["snapshot_sha256"]:
            raise ValueError("archived layout checksum mismatch")
        json.loads(raw)
        with Path(output).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return dict(exported=str(output), note="Layout exported only; no live terminals changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "status", "list", "recover", "export-layout"], nargs="?", default="status")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--checkpoint", help="checkpoint name from list; default: latest prior app run")
    parser.add_argument("--session", help="recover only this exact native session ID")
    parser.add_argument("--output", type=Path, help="export-layout: new output file (never overwritten)")
    parser.add_argument("--apply", action="store_true", help="recover: open missing conversations in new tabs")
    args = parser.parse_args()
    os.umask(0o077)
    app = Checkpoints(state=args.state)
    app.history.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(app.state, 0o700)
    lock = (app.state / ".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps(dict(ok=False, reason="checkpoint/recovery already running")))
        return 0 if args.action == "capture" else 1
    try:
        if args.action == "capture":
            result = app.capture()
        elif args.action == "status":
            result = load_json(app.state / "status.json")
            result["age_seconds"] = round(time.time() - result["captured_at"])
            result["overdue"] = result["age_seconds"] > 360
            try:
                error = load_json(app.state / "last-error.json")
                if error["checked_at"] > result["captured_at"]:
                    result["last_error"] = error
            except FileNotFoundError:
                pass
            result["healthy"] = not result["overdue"] and "last_error" not in result
        elif args.action == "list":
            result = [dict(checkpoint=p.name, captured_at=d["captured_at"], sessions=len(d["sessions"]),
                           unknown=len(d["unknown"])) for p, d in app.entries()]
        elif args.action == "recover":
            result = app.recovery(args.checkpoint, args.apply, args.session)
        else:
            result = app.export_layout(args.checkpoint, args.output)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        error = dict(ok=False, checked_at=time.time(), error=str(exc))
        # Error health is separate from the last successful checkpoint. A
        # stopped cmux or broken RPC never destroys the last usable archive.
        atomic_json(app.state / "last-error.json", error)
        print(json.dumps(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
