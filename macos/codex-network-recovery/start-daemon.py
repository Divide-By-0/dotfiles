#!/usr/bin/env python3
"""Start a missing daemon with adequate file capacity; never restart running work."""
import os
import shutil
from recover import raise_file_limit

raise_file_limit()
codex = shutil.which('codex')
if not codex:
    raise SystemExit('codex executable not found')
os.execv(codex, [codex, 'app-server', 'daemon', 'start'])
