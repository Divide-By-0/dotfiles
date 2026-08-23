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
  "restore --surface SURFACE-1")
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
fi

hardcoded_home="/Users/""aayushgupta"
if rg -n "$hardcoded_home" "$ROOT" --glob "!README.md"; then
  echo "tracked runtime source contains an untemplated home path" >&2
  exit 1
fi

git -C "$ROOT" diff --check
echo "agent-session-recovery tests passed"
