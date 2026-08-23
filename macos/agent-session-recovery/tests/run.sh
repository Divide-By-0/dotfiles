#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agent-session-recovery-tests.XXXXXX")"
TMUX_TEST_SOCKET=""

cleanup() {
  if [ -n "$TMUX_TEST_SOCKET" ]; then
    /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" kill-server >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

for file in "$ROOT"/bin/*.sh "$ROOT"/tmux/*.sh "$ROOT"/claude-hooks/*.sh "$ROOT/install.sh"; do
  bash -n "$file"
done
for file in "$ROOT/bin/cmux" "$ROOT/claude-hooks/moshi-claude-hook.sh" "$ROOT/claude-hooks/moshi-ghost-keeper.sh"; do
  sh -n "$file"
done
zsh -n "$ROOT/config/zsh-session-recovery.zsh"

mkdir -p "$TMP_ROOT/pycache"
PYTHONPYCACHEPREFIX="$TMP_ROOT/pycache" python3 -m py_compile "$ROOT"/claude-hooks/*.py

for template in "$ROOT"/launchagents/*.plist.in; do
  rendered="$TMP_ROOT/$(basename "${template%.in}")"
  sed "s|__HOME__|$HOME|g" "$template" >"$rendered"
  plutil -lint "$rendered" >/dev/null
done

fake_cmux="$TMP_ROOT/cmux-real"
fake_log="$TMP_ROOT/cmux.log"
cat >"$fake_cmux" <<'FAKE'
#!/bin/sh
printf "%s\n" "$*" >>"${FAKE_CMUX_LOG:?}"
case "$*" in
  "--json surface resume get --surface SURFACE-1")
    printf '%s\n' '{"restore_record":{"kind":"codex","checkpoint_id":"checkpoint-1"}}'
    ;;
  "--json surface resume get --surface SURFACE-2")
    printf '%s\n' '{"restore_record":{"kind":"claude","checkpoint_id":"checkpoint-2"}}'
    ;;
  "restore --surface SURFACE-1"|"restore --surface SURFACE-2")
    printf '%s\n' 'restored'
    ;;
  *)
    printf '%s\n' "passthrough:$*"
    ;;
esac
FAKE
chmod +x "$fake_cmux"

result="$(CMUX_REAL_BIN="$fake_cmux" CMUX_SURFACE_ID=SURFACE-1 FAKE_CMUX_LOG="$fake_log" "$ROOT/bin/cmux" restore codex checkpoint-1)"
[ "$result" = "restored" ]
tail -n 1 "$fake_log" | grep -Fqx "restore --surface SURFACE-1"

result="$(CMUX_REAL_BIN="$fake_cmux" CMUX_SURFACE_ID=SURFACE-2 FAKE_CMUX_LOG="$fake_log" "$ROOT/bin/cmux" restore claude checkpoint-2)"
[ "$result" = "restored" ]
tail -n 1 "$fake_log" | grep -Fqx "restore --surface SURFACE-2"

: >"$fake_log"
if CMUX_REAL_BIN="$fake_cmux" CMUX_SURFACE_ID=SURFACE-1 FAKE_CMUX_LOG="$fake_log" "$ROOT/bin/cmux" restore claude checkpoint-1 >/dev/null 2>&1; then
  echo "cmux shim accepted a stale or mismatched positional command" >&2
  exit 1
fi
if grep -Fq "restore --surface" "$fake_log"; then
  echo "cmux shim restored despite a mismatched record" >&2
  exit 1
fi

result="$(CMUX_REAL_BIN="$fake_cmux" FAKE_CMUX_LOG="$fake_log" "$ROOT/bin/cmux" version)"
[ "$result" = "passthrough:version" ]

plugin_root="$HOME/.tmux/plugins/tmux-resurrect"
if [ -d "$plugin_root/.git" ]; then
  mkdir -p "$TMP_ROOT/plugin/scripts"
  git -C "$plugin_root" show HEAD:scripts/restore.sh >"$TMP_ROOT/plugin/scripts/restore.sh"
  chmod +x "$TMP_ROOT/plugin/scripts/restore.sh"
  (
    cd "$TMP_ROOT/plugin"
    git apply --check "$ROOT/patches/tmux-resurrect-tmux-3.7-session-targets.patch"
    git apply "$ROOT/patches/tmux-resurrect-tmux-3.7-session-targets.patch"
  )
  grep -Fq 'list-windows -t "${session_name}:"' "$TMP_ROOT/plugin/scripts/restore.sh"
  grep -Fq 'has-session -t "${session_name}:"' "$TMP_ROOT/plugin/scripts/restore.sh"
fi

if [ -x /opt/homebrew/bin/tmux ]; then
  TMUX_TEST_SOCKET="agent-session-recovery-$$"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" -f /dev/null new-session -d -s ".projects.nosync"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" new-window -d -t ".projects.nosync:" -n second
  count="$(/opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" list-windows -t ".projects.nosync:" -F "#{window_index}" | wc -l | tr -d " ")"
  [ "$count" -eq 2 ]

  title_py="$ROOT/claude-hooks/moshi-cmux-title.py"
  [ "$(python3 "$title_py" --format-session "claude task" "/tmp/quests" active)" = "moshi-claude-task-quests" ]
  [ "$(python3 "$title_py" --format-session "claude task" "/tmp/quests" stale)" = "stale-moshi-claude-task-quests" ]
  [ "$(python3 "$title_py" --format-session "C20E58D9" "$HOME/.claude" stale)" = "stale-moshi-claude" ]

  state_dir="$TMP_ROOT/moshi-state"
  mkdir -p "$state_dir"
  stale_state="$state_dir/a6c6a8e027bd.json"
  stale_plist="$state_dir/a6c6a8e027bd.plist"
  plutil -create xml1 "$stale_plist"
  plutil -insert title -string "claude task" "$stale_plist"
  plutil -insert cwd -string "/tmp/quests" "$stale_plist"
  plutil -insert surface_id -string "A6C6A8E0-27BD-4A48-BAC5-B149E76E26CF" "$stale_plist"
  plutil -insert tmux_session -string "claude-task-a6c6a8" "$stale_plist"
  plutil -convert json -o "$stale_state" "$stale_plist"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" new-session -d -s "claude-task-a6c6a8" "sleep 120"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" new-session -d -s "claude-task-a6c6a8-x" "sleep 120"

  active_state="$state_dir/d665ab12a2db.json"
  active_plist="$state_dir/d665ab12a2db.plist"
  plutil -create xml1 "$active_plist"
  plutil -insert title -string "new parts" "$active_plist"
  plutil -insert cwd -string "/tmp/normal" "$active_plist"
  plutil -insert surface_id -string "D665AB12-A2DB-420E-94C1-04BD385CE70D" "$active_plist"
  plutil -insert tmux_session -string "new-parts-d665ab" "$active_plist"
  plutil -convert json -o "$active_state" "$active_plist"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" new-session -d -s "new-parts-d665ab" \
    "python3 -c 'import time; time.sleep(120)' moshi-cmux-mirror.py"

  ended_state="$state_dir/eeeeeeeeeeee.json"
  ended_plist="$state_dir/eeeeeeeeeeee.plist"
  plutil -create xml1 "$ended_plist"
  plutil -insert title -string "finished task" "$ended_plist"
  plutil -insert cwd -string "/tmp/normal" "$ended_plist"
  plutil -insert surface_id -string "EEEEEEEE-EEEE-4EEE-8EEE-EEEEEEEEEEEE" "$ended_plist"
  plutil -insert tmux_session -string "finished-task-eeeeee" "$ended_plist"
  plutil -insert stale -bool YES "$ended_plist"
  plutil -convert json -o "$ended_state" "$ended_plist"
  /opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" new-session -d -s "finished-task-eeeeee" \
    "python3 -c 'import time; time.sleep(120)' moshi-cmux-mirror.py"

  TMUX_BIN=/opt/homebrew/bin/tmux TMUX_SOCKET="$TMUX_TEST_SOCKET" \
    MOSHI_STATE_DIR="$state_dir" MOSHI_TITLE_PY="$title_py" \
    python3 "$ROOT/tmux/reconcile-moshi-sessions.py" --quiet
  sessions="$(/opt/homebrew/bin/tmux -L "$TMUX_TEST_SOCKET" list-sessions -F '#{session_name}')"
  printf '%s\n' "$sessions" | grep -Fqx "stale-moshi-claude-task-quests"
  printf '%s\n' "$sessions" | grep -Fqx "stale-moshi-claude-task-quests-2"
  printf '%s\n' "$sessions" | grep -Fqx "moshi-new-parts-normal"
  printf '%s\n' "$sessions" | grep -Fqx "stale-moshi-finished-task-normal"
  if printf '%s\n' "$sessions" | grep -Eq 'a6c6a8|d665ab|eeeeee'; then
    echo "Moshi reconciliation left an opaque surface id in a session name" >&2
    exit 1
  fi
fi

hardcoded_home="/Users/""aayushgupta"
if rg -n "$hardcoded_home" "$ROOT" --glob "!README.md"; then
  echo "tracked runtime source contains an untemplated home path" >&2
  exit 1
fi

git -C "$ROOT" diff --check
echo "agent-session-recovery tests passed"
