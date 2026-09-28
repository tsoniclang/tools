#!/usr/bin/env bash
set -euo pipefail
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  printf 'Usage: %s <container_dir>\n' "${0##*/}"
}

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
  usage
  exit 0
fi
if [[ $# -ne 1 || -z ${1:-} ]]; then
  usage >&2
  exit 2
fi

for dependency in git curl jq sort; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Missing dependency: %s\n' "$dependency" >&2
    exit 1
  fi
done

headers=('Accept: application/vnd.github+json' 'X-GitHub-Api-Version: 2026-03-10')
if [[ -n ${GITHUB_TOKEN:-} ]]; then
  if [[ $GITHUB_TOKEN == *$'\n'* || $GITHUB_TOKEN == *$'\r'* ]]; then
    printf 'GITHUB_TOKEN must not contain line breaks.\n' >&2
    exit 1
  fi
  headers+=("Authorization: Bearer $GITHUB_TOKEN")
else
  printf 'Discovering public repositories; set GITHUB_TOKEN to include accessible private repositories.\n'
fi

repositories=()
page=1
while :; do
  if ! response=$(printf '%s\n' "${headers[@]}" | curl --disable --fail --silent --show-error \
    --connect-timeout 10 --max-time 60 --header @- \
    "https://api.github.com/orgs/tsoniclang/repos?type=all&sort=full_name&direction=asc&per_page=100&page=$page"); then
    printf 'Repository discovery failed on page %s; nothing was cloned.\n' "$page" >&2
    exit 1
  fi
  if ! jq -e -s 'length == 1 and (.[0] | type == "array" and all(.[];
    (.name | type == "string" and test("^[A-Za-z0-9_.-]+$") and . != "." and . != "..")
    and .full_name == ("tsoniclang/" + .name)))' <<< "$response" >/dev/null; then
    printf 'Invalid repository listing on page %s; nothing was cloned.\n' "$page" >&2
    exit 1
  fi
  names=$(jq -r '.[].name' <<< "$response")
  if [[ -z $names ]]; then break; fi
  while IFS= read -r name; do repositories+=("$name"); done <<< "$names"
  page=$((page + 1))
done

if [[ ${#repositories[@]} -eq 0 ]]; then
  printf 'No tsoniclang repositories are visible to this request.\n' >&2
  exit 1
fi

mkdir -p -- "$1"
container_dir=$(cd -- "$1" && pwd -P)
names=$(printf '%s\n' "${repositories[@]}" | LC_ALL=C sort -u)
cloned=0
skipped=0
failed=0
while IFS= read -r name; do
  destination="$container_dir/$name"
  remote="git@github.com:tsoniclang/$name.git"
  if [[ -e $destination || -L $destination ]]; then
    origin=''
    if [[ -d $destination && ! -L $destination ]]; then
      root=$(git -C "$destination" rev-parse --show-toplevel 2>/dev/null || true)
      if [[ $root == "$destination" ]]; then
        origin=$(git -C "$destination" config --local --get remote.origin.url || true)
      fi
    fi
    case "${origin%.git}" in
      "git@github.com:tsoniclang/$name"|"https://github.com/tsoniclang/$name"|"ssh://git@github.com/tsoniclang/$name")
        printf 'Already present: %s (unchanged)\n' "$destination"
        skipped=$((skipped + 1))
        ;;
      *)
        printf 'Refusing existing path that is not the expected checkout: %s\n' "$destination" >&2
        failed=$((failed + 1))
        ;;
    esac
    continue
  fi
  if git clone -- "$remote" "$destination"; then
    cloned=$((cloned + 1))
  else
    printf 'Clone failed: %s\n' "$remote" >&2
    failed=$((failed + 1))
  fi
done <<< "$names"

printf '\nCloned: %s; already present: %s; failed: %s\n' "$cloned" "$skipped" "$failed"
[[ $failed -eq 0 ]]
