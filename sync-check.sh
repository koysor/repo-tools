#!/usr/bin/env bash
# Linux/macOS counterpart of sync-check.ps1: runs repo_status, then asks whether
# to run sync_repos, then install_workspace --update (all from this repo,
# repo-tools) against the folder that holds the sibling repos.
# Without an interactive terminal it skips the prompt and does not sync, unless
# -y/--yes is given.
set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") [-y|--yes]"
    echo "  -y, --yes   run sync_repos without the first prompt (it still asks per repo)"
}

yes=false
for arg in "$@"; do
    case "$arg" in
        -y | --yes) yes=true ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

project="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$project")"

if ! command -v uv > /dev/null; then
    echo "sync-check: uv was not found on PATH" >&2
    exit 2
fi

echo "== repo status + sync =="
echo

# repo_status exits 1 when something needs attention, which is a report, not a failure.
uv run --project "$project" "$project/repo_status.py" --fetch --root "$root"
if [ $? -eq 2 ]; then
    echo "warning: repo_status could not inspect one or more repos." >&2
fi

echo
run=false
if $yes; then
    run=true
elif [ -t 0 ] && [ -t 1 ]; then
    read -r -p "Run sync_repos? [y/N] " answer
    case "$answer" in
        [yY] | [yY][eE][sS]) run=true ;;
    esac
else
    echo "No interactive terminal; skipping sync_repos (use --yes to run it)."
fi

if $run; then
    uv run --project "$project" "$project/sync_repos.py" --root "$root"
fi

# Refresh the shared CLAUDE.md copy and install/update the koysor-skills plugin.
echo
uv run --project "$project" "$project/install_workspace.py" --update --root "$root"
