#!/usr/bin/python3
"""Private, bounded checkpoints of the macOS unified log stream."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
import uuid

MIB = 1024 * 1024
ROOT = Path.home() / "Library/Logs/SystemFlightRecorder"


def sync(fd):
    os.fsync(fd)
    # macOS also asks the drive to commit its write cache. Not available on Linux.
    if hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        sync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def tree_size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class Capture:
    def __init__(self, root, window=900, active_limit=512 * MIB,
                 archive_limit=2 * 1024 * MIB, segment_limit=16 * MIB):
        os.umask(0o077)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = (self.root / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("A recorder is already running")
        self.current = self.root / "current"
        self.archives = self.root / "incidents"
        self.archives.mkdir(exist_ok=True, mode=0o700)
        self.window = window
        self.active_limit = active_limit
        self.archive_limit = archive_limit
        self.segment_limit = segment_limit
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        if self.current.exists():
            # Never reuse/truncate the previous run, even on a same-boot restart.
            os.rename(self.current, self.archives / self.run_id)
        self.current.mkdir(mode=0o700)
        for directory in (self.root, self.archives):
            fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.prune_archives()
        self.started = time.time()
        self.segments = []
        self.handle = None
        self.evicted_bytes = 0
        self.total_bytes = 0
        self.sequence = 0
        self.last_checkpoint = 0
        self.stopping = False

    def prune_archives(self):
        entries = sorted((p for p in self.archives.iterdir() if p.is_dir()),
                         key=lambda p: p.name)
        sizes = {p: tree_size(p) for p in entries}
        total = sum(sizes.values())
        for index, p in enumerate(entries):
            if (len(entries) - index <= 16 and total <= self.archive_limit
                    and p.stat().st_mtime >= time.time() - 30 * 86400):
                continue
            shutil.rmtree(p)
            total -= sizes[p]

    def rotate(self, now):
        if self.handle:
            sync(self.handle.fileno())
            self.handle.close()
        self.sequence += 1
        path = self.current / ("%06d.log" % self.sequence)
        self.handle = path.open("ab", buffering=0)
        self.segments.append({"path": path.name, "start": now, "end": now, "bytes": 0})
        # Commit the directory entry for a newly created log file.
        fd = os.open(str(self.current), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, data, now=None):
        now = time.time() if now is None else now
        if (not self.handle or now - self.segments[-1]["start"] >= 60
                or self.segments[-1]["bytes"] >= self.segment_limit):
            self.rotate(now)
        self.handle.write(data)
        self.segments[-1]["bytes"] += len(data)
        self.segments[-1]["end"] = now
        self.total_bytes += len(data)
        self.prune(now)

    def prune(self, now):
        size = sum(s["bytes"] for s in self.segments)
        while len(self.segments) > 1:
            oldest = self.segments[0]
            too_old = oldest["end"] < now - self.window
            too_large = size > self.active_limit
            if not (too_old or too_large):
                break
            if too_large and not too_old:
                self.evicted_bytes += oldest["bytes"]
            (self.current / oldest["path"]).unlink()
            size -= oldest["bytes"]
            self.segments.pop(0)

    def checkpoint(self, state="running", source_pid=None):
        now = time.time()
        if self.handle:
            sync(self.handle.fileno())
        self.prune(now)
        atomic_json(self.current / "status.json", {
            "run_id": self.run_id, "recorder_pid": os.getpid(),
            "source_pid": source_pid, "started_unix": self.started,
            "checkpoint_unix": now, "state": state,
            "window_seconds": self.window, "active_limit_bytes": self.active_limit,
            "bytes_received": self.total_bytes,
            "bytes_evicted_early_by_size_cap": self.evicted_bytes,
            "segments": self.segments,
            "format": "macOS log stream compact; concatenate segments in filename order",
        })
        self.last_checkpoint = time.monotonic()

    def close(self):
        if self.handle:
            self.handle.close()
        self.lock.close()


def run(root=ROOT, command=None):
    capture = Capture(root)
    command = command or ["/usr/bin/log", "stream", "--style", "compact", "--level", "info"]
    source = None
    selector = selectors.DefaultSelector()

    def stop(_signum, _frame):
        capture.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Stay in launchd's process group so an abrupt recorder death does not
        # leave a detached log stream behind.
        source = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector.register(source.stdout, selectors.EVENT_READ)
        capture.checkpoint(source_pid=source.pid)
        while not capture.stopping:
            for key, _ in selector.select(timeout=1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    raise RuntimeError("System log stream ended unexpectedly")
                capture.write(data)
            if time.monotonic() - capture.last_checkpoint >= 5:
                capture.checkpoint(source_pid=source.pid)
        capture.checkpoint(state="stopped", source_pid=source.pid)
    except Exception as exc:
        # Do not send log contents to launchd stderr. Preserve an actionable status.
        capture.checkpoint(state="error: " + str(exc), source_pid=source.pid if source else None)
        raise
    finally:
        if source:
            source.terminate()
            try:
                source.wait(timeout=3)
            except subprocess.TimeoutExpired:
                source.kill()
                source.wait()
            source.stdout.close()
        selector.close()
        capture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print((args.root / "current/status.json").read_text())
    else:
        run(args.root)


if __name__ == "__main__":
    main()
