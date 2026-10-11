#!/bin/sh
# Hivemind Stop hook (Claude Code): at most once per session, blocks the stop
# once with a short reminder to write what another agent could reuse and to
# rate the entries the work tested. Registered in hooks/hooks.json.
#
# It only nudges a substantial session (at least HIVEMIND_STOP_MIN_TOOLS tool
# uses, default 20) that has not called hive_write or hive_feedback yet, and
# it fails open: any error, missing file or unparsable input exits 0 with no
# output. Plain POSIX sh, grep and sed: no jq, python or node. No network.
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

# (a) Once per session: the marker is written only when a stop is blocked.
dir="${TMPDIR:-/tmp}/hivemind-stop-reminder"
[ -e "$dir/$sid" ] && exit 0

# (e) Qualifying: enough tool uses, and no hive_write / hive_feedback yet.
grep -Eq '"name" *: *"[^"]*hive_(write|feedback)"' "$transcript" 2>/dev/null && exit 0
min="${HIVEMIND_STOP_MIN_TOOLS:-20}"
case "$min" in ''|*[!0-9]*) min=20 ;; esac
uses="$(grep -o '"type" *: *"tool_use"' "$transcript" 2>/dev/null | wc -l)" || exit 0
[ "${uses:-0}" -ge "$min" ] 2>/dev/null || exit 0

mkdir -p "$dir" 2>/dev/null && : > "$dir/$sid" 2>/dev/null || exit 0

printf '%s\n' '{"decision":"block","reason":"HIVEMIND: before finishing, write to Hivemind (hive_write) what another agent could reuse from this task, and rate the entries your work tested (hive_feedback). If nothing is new, write nothing and just finish."}'
exit 0
