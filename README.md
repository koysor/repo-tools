# repo-tools

Small scripts for working across a folder of sibling git repos.

## sync-check

`sync-check` runs the scripts below against the parent folder in one go: `repo_status.py --fetch`, then asks whether to run `sync_repos.py`, then `install_workspace.py --update`.

```powershell
# Windows, from the repos folder
.\repo-tools\sync-check.cmd        # or sync-check.ps1
.\repo-tools\sync-check.ps1 -Yes   # sync without the first prompt (sync_repos.py still asks per repo)
```

```bash
# Linux / macOS, from anywhere
repo-tools/sync-check.sh
repo-tools/sync-check.sh --yes
```

Without an interactive console it skips the sync prompt and does not sync unless `-Yes` / `--yes` is given. To keep running `.\sync-check.cmd` from the repos folder itself, add a forwarder there containing `@call "%~dp0repo-tools\sync-check.cmd" %*`.

## repo_status.py

Shows detailed git status for every git repo next to this one, without changing anything: branch, ahead/behind the upstream, staged, unstaged, untracked and conflicted files, stashes, any rebase/merge left half-finished, and the last commit. Clean repos are listed by name on one line, annotated when clean cannot be taken at face value (detached HEAD, no upstream, upstream gone, stashes).

```bash
uv run repo_status.py                # report from the last fetch
uv run repo_status.py --fetch        # fetch first so ahead/behind is current
uv run repo_status.py --max-files 0  # list every file (default: 10 per category)
uv run repo_status.py --root PATH    # scan PATH instead of this repo's parent (bare --root: the parent)
```

Exit codes: 0 everything clean, 1 something to commit, push, pull or finish, 2 a repo could not be inspected (takes precedence), 130 interrupted. Stashes are shown but do not make a repo unclean. It reuses the git plumbing in `sync_repos.py`, so the two scripts need to stay together.

## sync_repos.py

Checks every git repo next to this one for commits that exist on the remote but not locally, shows what is incoming, and offers to rebase onto the incoming commits.

```bash
uv run sync_repos.py              # fetch, report, then prompt per repo
uv run sync_repos.py --check      # report only; exit 1 if any repo is behind
uv run sync_repos.py --yes        # pull every eligible repo without asking
uv run sync_repos.py --root PATH  # scan PATH instead of this repo's parent (bare --root: the parent)
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

## review_prs.py

Lists the open pull requests of every sibling repo whose `origin` is on GitHub, summarises each one, recommends whether to merge it, asks per pull request, and squash-merges the ones you approve. It talks to GitHub through the [GitHub CLI](https://cli.github.com/), so `gh` must be installed and logged in (`gh auth login`).

```bash
uv run review_prs.py                  # report, then prompt per PR (default no)
uv run review_prs.py --check          # report only; never merges
uv run review_prs.py --yes            # merge every PR recommended for merge, without asking
uv run review_prs.py --json           # report as JSON; never merges
uv run review_prs.py --merge maths-python#9@<head sha> ...   # merge exactly these
uv run review_prs.py --max-files 0    # list every changed file (default: 10 per PR)
uv run review_prs.py --root PATH      # scan PATH instead of this repo's parent (bare --root: the parent)
```

For each PR the report shows the title, author, branch, size, age, changed files, CI checks and a verdict with its reasons:

| Verdict | When | Offered? |
|---|---|---|
| **merge** | Opened by a bot (e.g. Dependabot), not a draft, merges cleanly, every check passed or was skipped | Yes, recommended |
| **review** | Checks still running, no checks at all, mergeability not yet known, branch behind or protected, or opened by a person | Yes, flagged "needs review" |
| **blocked** | Draft, merge conflicts, a failed check, or changes requested | No |

At the prompt, `a(ll)` merges this PR and every remaining **merge**-verdict PR; **review** PRs are still asked about one by one. `--yes` merges **merge**-verdict PRs only.

### Safety

- Merges are `gh pr merge --squash --delete-branch --match-head-commit <sha>`, where `<sha>` is the head commit shown in the report: if anyone pushes after the report, GitHub refuses the merge.
- `-R owner/name` is always passed, so gh never checks out, pulls or deletes branches in your local clones. After merging, the summary reminds you to pull with `sync_repos.py`.
- `--merge REPO#N@SHA` re-fetches each PR first and skips it if its head is no longer `SHA` or it is now blocked. The `merge_arg` field of `--json` gives the exact argument.
- A repo with no `origin`, a non-GitHub origin, or a failing `gh` call is listed under "Needs attention" instead of hiding the others.
- gh runs with prompts disabled and a timeout. Without a terminal the script behaves like `--check` unless `--yes` is given.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No open PRs, or everything approved was merged |
| 1 | `--check`/`--json` found open PRs, a merge failed, or a `--merge` PR was skipped |
| 2 | `--check`/`--json` could not inspect a repo, or git/gh is missing or not logged in |
| 130 | Interrupted with Ctrl-C |

### Claude skill

`skills/review-prs/SKILL.md` builds on the script: Claude runs `--json`, reads each PR's diff and release notes, writes a short risk summary next to the script's verdict, asks which PRs to merge, and merges only those with `--merge`. Ask Claude Code to "review the open PRs" from `C:\Repos` or any repo. It is part of the `koysor-skills` plugin below.

## Machine setup: install_workspace.py

Run once on each machine after cloning, and again whenever you like; it only changes what is missing or out of date. It does two things:

1. Copies `workspace/CLAUDE.md` (the Claude Code guidance for the folder that holds all the sibling repos: layout, shared conventions, cross-repo commands) into that parent folder, backing up a differing copy to `CLAUDE.md.bak`.
2. Makes sure the `koysor-skills` plugin below is installed at user scope: adds the `repo-tools` marketplace (`koysor/repo-tools` on GitHub) and installs the plugin if either is missing. A disabled plugin is reported, not re-enabled. Skipped if `claude` is not on PATH.

```bash
uv run install_workspace.py             # set up anything missing
uv run install_workspace.py --update    # same, then update the marketplace and plugin to the latest push
uv run install_workspace.py --check     # report only; exit 1 if anything is missing or out of date
uv run install_workspace.py --no-plugin # only the CLAUDE.md copy
uv run install_workspace.py --root PATH
```

Exit codes: 0 set up, 1 `--check` found something missing, 2 a `claude` command failed or `--root` is not a folder. `sync-check.ps1` runs it with `--update`.

Edit `workspace/CLAUDE.md` here, commit it and re-run the script; never edit the copy. It is a copy rather than an `@repo-tools/workspace/CLAUDE.md` import because Claude Code asks for approval of an import from outside the project once in every repo, and ignores it until then.

## Shared Claude Code skills (`koysor-skills` plugin)

This repo is also a Claude Code plugin marketplace. Skills in `skills/` are shared by every repo once the plugin is installed at user scope; skills that only make sense in one repo stay in that repo's `.claude/skills/`.

| Skill | What it does |
|---|---|
| `koysor-skills:review-prs` | Reviews and merges open PRs across the sibling repos with `review_prs.py` |
| `koysor-skills:marimo-helper` | Rules for marimo notebooks: unique globals, reactive dependencies, browser limits |

`install_workspace.py` installs it (see above). To do it by hand instead:

```bash
claude plugin marketplace add koysor/repo-tools
claude plugin install koysor-skills@repo-tools --scope user
```

To change a skill, edit it in `skills/`, bump `version` in `.claude-plugin/plugin.json`, commit and push, then pick it up on each machine with `uv run install_workspace.py --update` (or `sync-check`), which runs `claude plugin marketplace update repo-tools` and `claude plugin update koysor-skills@repo-tools`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

The tests build throwaway local bare remotes and clones, so they need no network.
