#!/usr/bin/env bash
# Create a pull request in this fork only.
#
# The upstream remote is a fetch/merge source. It must never be a PR target.
# Usage: ./scripts/create-pr.sh --title "..." --body "..."

set -euo pipefail

readonly expected_repo="dj-oyu/bambuddy"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

normalize_repo() {
    local url="$1"
    url="${url%.git}"
    case "$url" in
        https://github.com/*) printf '%s\n' "${url#https://github.com/}" ;;
        git@github.com:*) printf '%s\n' "${url#git@github.com:}" ;;
        ssh://git@github.com/*) printf '%s\n' "${url#ssh://git@github.com/}" ;;
        *) return 1 ;;
    esac
}

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

origin_repo=$(normalize_repo "$(git remote get-url origin)") || fail "origin must be a GitHub remote"
[[ "$origin_repo" == "$expected_repo" ]] || fail "origin is $origin_repo, expected $expected_repo"

branch=$(git branch --show-current)
[[ -n "$branch" ]] || fail "detached HEAD cannot be used to create a PR"
[[ "$branch" != "main" ]] || fail "create PRs from a feature branch, not main"

git diff --quiet && git diff --cached --quiet || fail "commit or stash local changes before creating a PR"

for arg in "$@"; do
    case "$arg" in
        --repo|--repo=*|--base|--base=*|--head|--head=*)
            fail "$arg is fixed by this wrapper; PRs target $expected_repo:main"
            ;;
    esac
done

exec gh pr create --repo "$expected_repo" --base main --head "$branch" "$@"
