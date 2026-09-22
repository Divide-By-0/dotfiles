#!/bin/sh
# Ghost pane keeper for cmux → Moshi binds.
# Runs a live cmux terminal mirror so phone/Moshi clients see and drive the
# real Claude/Codex TUI (not a status stub). Also drains the per-surface hook
# queue so Moshi binds stay attached to this pane.
set -u
state_file="${1:-}"
queue_dir="${2:-}"

MIRROR="${HOME}/.claude/hooks/moshi-cmux-mirror.py"
LOG="${HOME}/.claude/hooks/moshi-sessionstart.log"

log() {
  echo "---- $(date '+%Y-%m-%dT%H:%M:%S%z') ----" >>"$LOG" 2>/dev/null || true
  echo "$*" >>"$LOG" 2>/dev/null || true
}

if [ ! -f "$MIRROR" ]; then
  log "mirror missing: $MIRROR"
  echo "moshi-cmux-mirror.py missing; cannot mirror cmux surface" >&2
  sleep 5
  exit 1
fi

# Ensure queue dir exists even if state is incomplete.
if [ -n "$queue_dir" ]; then
  mkdir -p "$queue_dir" 2>/dev/null || true
fi

export PYTHONUNBUFFERED=1
exec python3 "$MIRROR" "$state_file" "$queue_dir"
