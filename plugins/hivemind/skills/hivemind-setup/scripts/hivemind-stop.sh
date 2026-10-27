#!/bin/sh
# Hivemind Stop hook (Claude Code, Codex, GitHub Copilot CLI; Gemini CLI as an
# AfterAgent hook):
# at most once per session, blocks the stop once with a short reminder to
# write what another agent could reuse and to rate the entries the work
# tested. Registered in hooks/hooks.json (Gemini CLI: the extension's AfterAgent hook).
#
# It only nudges a substantial session (at least HIVEMIND_STOP_MIN_TOOLS tool
# uses, default 20) that has not called hive_write or hive_feedback yet, or
# that read entries (hive_search, hive_get) and never rated any, and it
# fails open: any error, missing file or unparsable input exits 0 with no
# output. Plain POSIX sh, grep, sed and sort: no jq, python or node. No network.
#
# Stop input (stdin JSON): session_id, transcript_path, stop_hook_active.
# Block output: {"decision":"block","reason":"..."} on stdout, exit 0.

# (c) Incognito (ADR 0035), detected as hivemind-session-start.sh does.
case "$(printf '%s' "${HIVEMIND_INCOGNITO:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) exit 0 ;;
esac

# (d) Hivemind is not configured for this session.
[ -n "${HIVEMIND_API_KEY:-}" ] || exit 0

[ -t 0 ] && exit 0
input="$(cat 2>/dev/null)" || exit 0

# (b) Already continuing because of a stop hook.
printf '%s' "$input" | grep -q '"stop_hook_active" *: *true' && exit 0

field() {
  printf '%s' "$input" | grep -o "\"$1\" *: *\"[^\"]*\"" | head -n 1 \
    | sed 's/^[^:]*: *"//; s/"$//'
}
sid="$(field session_id | tr -c 'A-Za-z0-9_-' '_')"
transcript="$(field transcript_path)"
[ -n "$sid" ] && [ -n "$transcript" ] && [ -r "$transcript" ] || exit 0

# (a) Once per session: the marker is written only when a stop is blocked. It
# lives in a private per-user folder: $XDG_RUNTIME_DIR/hivemind-stop-reminder
# when that is ours, else ${TMPDIR:-/tmp}/hivemind-stop-reminder-<uid>. A
# folder that is not a real directory of ours, or is group/other writable,
# is refused (no reminder), so another user cannot suppress or spy on it.
if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "$XDG_RUNTIME_DIR" ] \
  && [ ! -L "$XDG_RUNTIME_DIR" ] && [ -O "$XDG_RUNTIME_DIR" ]; then
  dir="$XDG_RUNTIME_DIR/hivemind-stop-reminder"
else
  uid="$(id -u 2>/dev/null)" && [ -n "$uid" ] || exit 0
  dir="${TMPDIR:-/tmp}/hivemind-stop-reminder-$uid"
fi
[ -e "$dir" ] || [ -L "$dir" ] || mkdir -m 700 "$dir" 2>/dev/null
private() {
  [ -d "$dir" ] && [ ! -L "$dir" ] && [ -O "$dir" ] || return 1
  mode="$(ls -ld "$dir" 2>/dev/null)" || return 1
  case "$mode" in d????w*|d???????w*) return 1 ;; esac
}
private || exit 0
[ -e "$dir/$sid" ] && exit 0

# (e) Qualifying: enough tool uses, and either no hive_write / hive_feedback
# yet, or entries read (hive_search / hive_get) and no hive_feedback.
# Claude Code records the tool list in prompt_snapshot attachments (and
# deferred_tools_record), as {"name":...,"schema":{...}} definitions, so
# lines that define tools are skipped: a definition is not a call.
calls() {
  grep -Ev '"(input_schema|inputSchema|schema|parameters)" *: *\{|"type" *: *"(prompt_snapshot|deferred_tools_record)"' \
    "$transcript" 2>/dev/null
}
calls | grep -Eq '"name" *: *"[^"]*hive_feedback"' && exit 0
if calls | grep -Eq '"name" *: *"[^"]*hive_write"'; then
  calls | grep -Eq '"name" *: *"[^"]*hive_(search|get)"' || exit 0
fi
min="${HIVEMIND_STOP_MIN_TOOLS:-20}"
case "$min" in ''|*[!0-9]*) min=20 ;; esac
# Claude Code records a call as "tool_use"; Codex as "function_call",
# "custom_tool_call" or "local_shell_call" (their outputs end in "_output");
# GitHub Copilot CLI writes one "tool.execution_start" event per call.
uses="$(calls | grep -Eo '"type" *: *"(tool_use|function_call|custom_tool_call|local_shell_call|tool\.execution_start)"' \
  | wc -l)" || exit 0
# Gemini CLI rewrites a call's record as it progresses, so count each
# functionResponse id once.
gemini="$(calls | grep -Eo '"functionResponse" *: *\{ *"id" *: *"[^"]*"' \
  | sort -u | wc -l)" || exit 0
uses=$((uses + gemini))
[ "${uses:-0}" -ge "$min" ] 2>/dev/null || exit 0

private && (umask 077; : > "$dir/$sid") 2>/dev/null || exit 0

printf '%s\n' '{"decision":"block","reason":"HIVEMIND: before finishing, write to Hivemind (hive_write) what another agent could reuse from this task, and rate the entries your work tested (hive_feedback). If nothing is new, write nothing and just finish."}'
exit 0
