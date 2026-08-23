#!/bin/bash
# agent-window-name.sh — descriptive tmux window name for Claude Code / Codex sessions.
#
# Wired as a UserPromptSubmit hook for BOTH agents (claude settings.json + codex
# hooks.json), alongside moshi's hooks. On each prompt it sets the tmux window name
# to "<2 keywords from the prompt> · <dir basename>", e.g.  fix-auth · benny
#
# WHY a hook (not zsh): while an agent runs, the zsh prompt is blocked, so the
# .zshrc _tmux_auto_rename_window precmd can't fire — the window would otherwise
# stay stuck on the plain dir name. This hook fills that gap. When the agent EXITS
# and the shell prompt returns, that same precmd reclaims the window and renames it
# back to just the dir basename — so we intentionally do NOT revert here.
#
# HARD RULE: emit NOTHING on stdout. Claude Code injects a UserPromptSubmit hook's
# stdout into the model's context; any output here would pollute the conversation.
# All real work is silent; diagnostics go to the debug log only.
set -u

# REASON: hook may be invoked with an unreadable ~/Documents cwd (macOS TCC on
# launchd/mosh-hosted panes — see reference_tmux_documents_tcc). We read the dir
# from the hook JSON, never from $PWD, so hop somewhere always-readable first so
# jq/tmux never stat() an EPERM cwd.
cd "$HOME" 2>/dev/null || true

DEBUG_LOG="$HOME/.tmux/agent-window-name.log"
INPUT=$(cat)

# Only meaningful inside tmux.
[ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || exit 0

# Prompt text: Claude uses .prompt; try a few shapes so Codex works too.
PROMPT=$(printf '%s' "$INPUT" | jq -r '[.prompt, .user_prompt, .message, .text, .input] | map(select(type=="string" and length>0)) | first // empty' 2>/dev/null)

# Working dir: prefer the JSON, else resolve the pane's real cwd through secretty.
CWD=$(printf '%s' "$INPUT" | jq -r '(.cwd // .working_directory // .workdir) // empty' 2>/dev/null)
if [ -z "$CWD" ]; then
  ppid=$(tmux display-message -p -t "$TMUX_PANE" '#{pane_pid}' 2>/dev/null)
  [ -n "$ppid" ] && CWD=$("$HOME/.tmux/real-cwd.sh" "$ppid" 2>/dev/null)
fi
DIR=$(basename "${CWD:-agent}")

# 2 keyword slug: lowercase, strip punctuation, drop stopwords (NOT task verbs like
# fix/add/parse), keep first 2 words >=2 chars, join with '-'.
SLUG=$(printf '%s' "$PROMPT" \
  | tr '[:upper:]' '[:lower:]' \
  | tr -c 'a-z0-9 ' ' ' \
  | awk '{
      for (i=1;i<=NF;i++) {
        w=$i
        if (length(w) < 2) continue
        if (w ~ /^(the|a|an|to|in|on|at|of|for|and|or|is|are|am|be|as|it|its|this|that|these|those|my|me|i|we|our|us|you|your|please|with|into|from|do|does|did|will|would|should|shall|can|could|about|there|their|then|than|was|were|has|have|had|but|not|all|any|just|now|here|so|if|by|up|out|over|via|per|etc|why|what|when|where|who|how|which|whom|whose)$/) continue
        print w; c++
        if (c==2) exit
      }
    }' \
  | paste -sd '-' - )

# Nothing usable extracted (empty/all-stopword prompt) -> leave the name alone.
[ -n "$SLUG" ] || exit 0

NAME="$SLUG · $DIR"
tmux rename-window -t "$TMUX_PANE" "$NAME" 2>/dev/null

# Small rolling debug log (helps confirm Codex's JSON shape parses too).
{ printf '%s pane=%s name=%q prompt=%.60s\n' "$(tmux display -p '#{t:client_activity}' 2>/dev/null)" "$TMUX_PANE" "$NAME" "$PROMPT"; } >> "$DEBUG_LOG" 2>/dev/null
tail -n 200 "$DEBUG_LOG" > "$DEBUG_LOG.tmp" 2>/dev/null && mv "$DEBUG_LOG.tmp" "$DEBUG_LOG" 2>/dev/null
exit 0
