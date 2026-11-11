#!/bin/sh
# Compares the installed Hivemind plugin, and the copies of its text that
# setup put outside it, with the latest release. Reads only and changes
# nothing: the fetched release is compared, never run. Plain POSIX sh, git,
# diff, sed and awk.
set -u

usage() {
  cat <<'EOF'
Usage: hivemind-drift.sh [options]

Compares the installed Hivemind plugin and your copies of its text with the
latest release. Changes nothing.

  --repo URL        repository to compare with: any git URL or path. Default:
                    the installed plugin's own git remote when it is a clone,
                    else the first line of ~/.config/hivemind/plugin-source,
                    else https://github.com/monochrome-muncher/hivemind
  --ref REF         tag or branch to compare with (default: the newest vX.Y.Z tag)
  --latest DIR      compare with this checkout of the repository instead of fetching
  --keep DIR        fetch into DIR (new or empty) and keep it, to copy files from
  --installed DIR   the installed plugin (default: the one this script is in)
  --block FILE      a file that should hold the <!-- hivemind:begin --> block
  --skills DIR      a folder with copies of the plugin's skill folders
  --copy FILE=PATH  a copied file or folder, and its path inside the plugin
  --print-block     print the latest instruction block and stop

Exit status: 0 no drift, 1 drift found, 2 could not compare.
EOF
}

die() {
  printf 'hivemind-drift: %s\n' "$*" >&2
  exit 2
}

nl='
'
public_repo=https://github.com/monochrome-muncher/hivemind
source_file=${XDG_CONFIG_HOME:-${HOME:-}/.config}/hivemind/plugin-source
repo=
repo_from="--repo"
ref=
latest=
keep=
installed=
checks=
print_block=0
while [ $# -gt 0 ]; do
  case $1 in
    --repo | --ref | --latest | --keep | --installed | --block | --skills | --copy)
      [ $# -ge 2 ] || die "$1 needs a value"
      case $1 in
        --repo) repo=$2 ;;
        --ref) ref=$2 ;;
        --latest) latest=$2 ;;
        --keep) keep=$2 ;;
        --installed) installed=$2 ;;
        *) checks="$checks${1#--} $2$nl" ;;
      esac
      shift 2
      ;;
    --print-block) print_block=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

scratch=$(mktemp -d "${TMPDIR:-/tmp}/hivemind-drift.XXXXXX") || die "cannot create a temporary folder"
trap 'rm -rf "$scratch"' EXIT
trap 'exit 2' HUP INT TERM

shown() {
  case $1 in
    "${HOME:-/}"/?*) printf '~%s' "${1#"${HOME:-}"}" ;;
    *) printf '%s' "$1" ;;
  esac
}

version_of() {
  for manifest in package.json plugin.json .claude-plugin/plugin.json gemini-extension.json; do
    [ -f "$1/$manifest" ] || continue
    sed -n 's/^.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1/$manifest" | head -n 1
    return
  done
  [ -f "$1/plugin.yaml" ] && sed -n 's/^version:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$1/plugin.yaml" | head -n 1
}

# Prints -1, 0 or 1 as version $1 is older than, equal to or newer than $2.
compare_versions() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    na = split(a, x, "."); nb = split(b, y, ".")
    for (i = 1; i <= (na > nb ? na : nb); i++) {
      if (x[i] + 0 < y[i] + 0) { print -1; exit }
      if (x[i] + 0 > y[i] + 0) { print 1; exit }
    }
    print 0
  }'
}

# A URL without any user:password@ part, for printing.
without_credentials() {
  printf '%s' "$1" | sed 's#^\([a-zA-Z][a-zA-Z0-9+.-]*://\)[^/@]*@#\1#'
}

# The remote of a clone of the Hivemind repository that holds $1, if any.
own_remote() {
  top=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ -f "$top/plugins/hivemind/skills/hivemind-setup/SKILL.md" ] ||
    [ -f "$top/skills/hivemind-setup/SKILL.md" ] || return 1
  git -C "$top" remote get-url origin 2>/dev/null ||
    git -C "$top" remote get-url "$(git -C "$top" remote | head -n 1)" 2>/dev/null
}

choose_repo() {
  [ -n "$repo" ] && return
  if [ -n "$installed" ] && repo=$(own_remote "$installed") && [ -n "$repo" ]; then
    repo_from="the installed plugin's git remote"
  elif [ -r "$source_file" ] &&
    repo=$(sed -n '/^[[:space:]]*#/d; s/^[[:space:]]*//; s/[[:space:]]*$//; /./{p;q;}' "$source_file") &&
    [ -n "$repo" ]; then
    repo_from=$(shown "$source_file")
  else
    repo=$public_repo
    repo_from="the default"
  fi
}

newest_release_tag() {
  GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs "$repo" 'v*' 2>/dev/null |
    sed -n 's#^.*refs/tags/v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p' |
    awk -F. '{ printf "%09d.%09d.%09d v%s\n", $1, $2, $3, $0 }' |
    sort | tail -n 1 | cut -d' ' -f2
}

fetch_latest() {
  command -v git >/dev/null 2>&1 || die "git is not installed; pass --latest with a checkout of the repository"
  choose_repo
  shown_repo=$(without_credentials "$repo")
  if [ -z "$ref" ]; then
    ref=$(newest_release_tag)
    [ -n "$ref" ] || die "found no vX.Y.Z tag at $shown_repo ($repo_from): it is unreachable, a mirror without tags (pass --ref <branch>), or not the right address (pass --repo, or --latest with a checkout)"
  fi
  GIT_TERMINAL_PROMPT=0 git -c advice.detachedHead=false clone --quiet --depth 1 \
    --branch "$ref" "$repo" "$1" 2>/dev/null || die "could not fetch $ref from $shown_repo ($repo_from)"
}

# The marked blocks of a file, without carriage returns or trailing blanks.
blocks_of() {
  sed 's/\r$//; s/[[:space:]]*$//' "$1" |
    awk '/^<!-- hivemind:begin -->$/ { inside = 1 } inside { print } /^<!-- hivemind:end -->$/ { inside = 0 }'
}

count_lines() {
  grep -c "^$1\$" "$2" 2>/dev/null
}

drift=0
report() {
  printf '%s\n' "$*"
}
drifted() {
  drift=$((drift + 1))
  report "$@"
}

if [ -z "$installed" ]; then
  here=$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)
  [ -n "$here" ] && [ -n "$(version_of "$here")" ] && installed=$here
fi

if [ -n "$latest" ]; then
  latest=$(cd "$latest" 2>/dev/null && pwd) || die "no such folder: $latest"
  source_label="the checkout $(shown "$latest")"
else
  if [ -n "$keep" ]; then
    latest=$keep
  else
    latest=$scratch/latest
  fi
  fetch_latest "$latest"
  source_label="$ref of $shown_repo (from $repo_from)"
fi
latest_plugin=$latest/plugins/hivemind
[ -f "$latest_plugin/skills/hivemind-setup/SKILL.md" ] ||
  die "$(shown "$latest") is not a checkout of the Hivemind repository"

canonical=$scratch/block
blocks_of "$latest_plugin/skills/hivemind-setup/SKILL.md" >"$canonical"
[ "$(count_lines '<!-- hivemind:begin -->' "$canonical")" = 1 ] ||
  die "the latest hivemind-setup skill has no single instruction block"
if [ "$print_block" = 1 ]; then
  cat "$canonical"
  exit 0
fi

latest_version=$(version_of "$latest_plugin")
report "Latest: version ${latest_version:-unknown}, $source_label"

# Where a file of the installed plugin lives in the repository: the
# Antigravity plugin is a folder of its own next to plugins/hivemind.
latest_counterpart() {
  if [ -e "$latest_plugin/$1" ]; then
    printf '%s' "$latest_plugin/$1"
  else
    printf '%s' "$latest/plugins/antigravity/$1"
  fi
}

compare_file() { # installed latest label
  if [ ! -e "$1" ]; then
    drifted "  missing: $3"
  elif cmp -s "$1" "$2"; then
    :
  else
    drifted "  differs: $3"
  fi
}

compare_dir() { # installed latest label
  if [ ! -d "$1" ]; then
    drifted "  missing: $3"
    return
  fi
  diff -rq -x __pycache__ -x .DS_Store "$1" "$2" >"$scratch/dirdiff" 2>&1
  while IFS= read -r line; do
    case $line in
      "Only in $1"*)
        rest=${line#"Only in $1"}
        drifted "  only in your copy: $3${rest%%: *}/${rest#*: }"
        ;;
      "Only in $2"*)
        rest=${line#"Only in $2"}
        drifted "  missing: $3${rest%%: *}/${rest#*: }"
        ;;
      "Files $1"*)
        rest=${line#"Files $1"}
        drifted "  differs: $3${rest%% and *}"
        ;;
      *) drifted "  $line" ;;
    esac
  done <"$scratch/dirdiff"
}

compare_skills() { # folder holding the skill folders, label prefix
  for skill in "$latest_plugin"/skills/*/; do
    name=$(basename "$skill")
    compare_dir "$1/$name" "${skill%/}" "$2$name"
  done
}

if [ -z "$installed" ]; then
  report "Installed plugin: none around this script (for copied skills, pass --skills)"
elif [ ! -d "$installed/skills" ]; then
  die "$(shown "$installed") has no skills folder"
else
  installed_version=$(version_of "$installed")
  if [ -z "$installed_version" ] || [ -z "$latest_version" ]; then
    state="version unknown"
  else
    case $(compare_versions "$installed_version" "$latest_version") in
      -1) state="behind"; drift=$((drift + 1)) ;;
      0) state="current" ;;
      1) state="newer than the release compared with" ;;
    esac
  fi
  report "Installed plugin: $(shown "$installed"), version ${installed_version:-unknown}: $state"
  compare_skills "$installed/skills" "skills/"
  for file in guidelines/hivemind.md cursor/rules/hivemind.mdc rules/AGENTS.md; do
    [ -e "$installed/$file" ] && compare_file "$installed/$file" "$(latest_counterpart "$file")" "$file"
  done
fi

check_block() {
  label="Instruction block in $(shown "$1")"
  if [ ! -f "$1" ]; then
    report "$label: no such file"
    return
  fi
  found=$scratch/found
  blocks_of "$1" >"$found"
  begins=$(count_lines '<!-- hivemind:begin -->' "$found")
  ends=$(count_lines '<!-- hivemind:end -->' "$found")
  if [ "$begins" = 0 ]; then
    report "$label: none"
  elif [ "$begins" != 1 ] || [ "$ends" != 1 ]; then
    drifted "$label: $begins begin and $ends end markers (keep exactly one block)"
  elif cmp -s "$found" "$canonical"; then
    report "$label: current"
  else
    drifted "$label: differs"
    diff -u -L "$(shown "$1")" -L "latest" "$found" "$canonical"
  fi
}

check_copy() {
  installed_path=${1%%=*}
  bundle_path=${1#*=}
  [ "$installed_path" != "$1" ] && [ -n "$bundle_path" ] || die "--copy needs FILE=PATH"
  source=$latest_plugin/$bundle_path
  [ -e "$source" ] || die "the latest plugin has no $bundle_path"
  before=$drift
  report "Copy $(shown "$installed_path") of $bundle_path:"
  if [ -d "$source" ]; then
    compare_dir "$installed_path" "$source" "$bundle_path"
  else
    compare_file "$installed_path" "$source" "$bundle_path"
    if [ "$drift" != "$before" ] && [ -f "$installed_path" ]; then
      diff -u -L "$(shown "$installed_path")" -L "latest" "$installed_path" "$source"
    fi
  fi
  [ "$drift" = "$before" ] && report "  current"
}

printf '%s' "$checks" >"$scratch/checks"
while IFS= read -r check; do
  kind=${check%% *}
  value=${check#* }
  case $kind in
    block) check_block "$value" ;;
    skills)
      report "Skill copies in $(shown "$value"):"
      before=$drift
      compare_skills "$value" ""
      [ "$drift" = "$before" ] && report "  current"
      ;;
    copy) check_copy "$value" ;;
  esac
done <"$scratch/checks"

if [ "$drift" = 0 ]; then
  report "No drift."
  exit 0
fi
report "Drift: $drift difference(s)."
exit 1
