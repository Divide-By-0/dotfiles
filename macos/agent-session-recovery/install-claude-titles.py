#!/usr/bin/env python3
"""Install only the Claude title hook, preserving all existing user settings."""
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile


def install(home: Path) -> None:
    root = Path(__file__).resolve().parent
    source = root / "claude-hooks/claude-terminal-title.py"
    target = home / ".claude/hooks/claude-terminal-title.py"
    settings = home / ".claude/settings.json"
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Claude settings must be an object")
    command = 'python3 "$HOME/.claude/hooks/claude-terminal-title.py"'
    data.setdefault("env", {})["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        groups = hooks.setdefault(event, [])
        if not any(h.get("command") == command for group in groups for h in group.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 2}]})
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != source.read_bytes():
        shutil.copy2(target, str(target) + ".before-title-hook." + stamp)
    if target.is_symlink():
        target.unlink()
    shutil.copyfile(source, target)
    target.chmod(0o755)
    text = json.dumps(data, indent=2) + "\n"
    if settings.exists() and settings.read_text() == text:
        return
    if settings.exists():
        shutil.copy2(settings, str(settings) + ".before-title-hook." + stamp)
    with tempfile.NamedTemporaryFile(mode="w", dir=settings.parent, delete=False) as output:
        output.write(text)
        temporary = Path(output.name)
    temporary.chmod(settings.stat().st_mode & 0o777 if settings.exists() else 0o600)
    os.replace(temporary, settings)


if __name__ == "__main__":
    install(Path.home())
