# repo-tools

Small scripts for working across a folder of sibling git repos.

## sync_repos.py

Checks every git repo next to this one for commits that exist on the remote but not locally, shows what is incoming, and offers to rebase onto the incoming commits.

```bash
uv run sync_repos.py              # fetch, report, then prompt per repo
uv run sync_repos.py --check      # report only; exit 1 if any repo is behind
uv run sync_repos.py --yes        # pull every eligible repo without asking
uv run sync_repos.py --root PATH  # scan PATH instead of this repo's parent
uv run sync_repos.py --no-fetch   # compare against the last fetch
```

It works from anywhere with `uv run --project <path to repo-tools> sync_repos.py`.

### What it does

1. Finds the git repos in the parent directory (or `--root`) and runs `git fetch --prune` in each, in parallel.
2. For each repo that is behind its upstream, lists the incoming commits (hash, date, author, subject) and a diffstat.
3. Asks `Rebase <repo> onto <upstream>? [y/N/a(ll)/q(uit)]`, defaulting to no.
4. Runs `git rebase --rebase-merges <sha>` for the repos you approve, where `<sha>` is the exact upstream commit it showed you (commits pushed since the report are not pulled, and `pull.*` config is ignored), then prints a summary: old and new commit, commits pulled, files changed, and a reminder to run `uv sync` when `pyproject.toml` or `uv.lock` changed.

### Safety

- Repos with uncommitted changes are skipped and reported; nothing is stashed.
- A repo with unpushed local commits is flagged, and the prompt warns that they will be rebased.
- If a rebase hits conflicts it is aborted with `git rebase --abort`, so no repo is left mid-rebase. The summary reports it and the exit code is 1.
- A repo that is already mid-rebase, merge, cherry-pick, revert or bisect is reported under "Needs attention" and never touched.
- If any git command fails or times out, the repo is reported under "Needs attention" instead of being treated as clean or up to date.
- Git runs with stdin closed and no controlling terminal, so credential and ssh prompts fail rather than hang.
- Repos with no upstream, a detached HEAD, or a failed fetch are listed under "Needs attention".
- Without a terminal (piped input, cron) the script behaves like `--check` unless `--yes` is given.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing behind, or everything approved was pulled |
| 1 | `--check` found a repo behind, or a rebase failed or hit a conflict |
| 2 | `--check` could not check a repo (fetch failed, no upstream, detached HEAD, ...), or git is missing; takes precedence over 1 |
| 130 | Interrupted with Ctrl-C (the summary of repos already updated is still printed) |

Requires git 2.21 or newer.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

The tests build throwaway local bare remotes and clones, so they need no network.
