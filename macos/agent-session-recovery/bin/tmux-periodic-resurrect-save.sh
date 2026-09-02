#!/bin/bash
set -euo pipefail

: "${HOME:?HOME must be set by launchd or the calling shell}"

exec "$HOME/.local/bin/tmux-daily-resurrect-save.sh" --checkpoint-only
