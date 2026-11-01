# Repo maintenance guide

What was done across the repos in `C:\Repos`, why, and the git and tooling concepts behind it.

- [1. The repos](#1-the-repos)
- [2. What was done, in order](#2-what-was-done-in-order)
- [3. Git concepts explained](#3-git-concepts-explained)
- [4. Python tooling concepts](#4-python-tooling-concepts)
- [5. CI and GitHub concepts](#5-ci-and-github-concepts)
- [6. The sync_repos.py script](#6-the-sync_repospy-script)
- [7. Cheat sheet](#7-cheat-sheet)

---

## 1. The repos

| Repo | Purpose |
|---|---|
| `quant-finance` | Python code and Streamlit apps for fixed income, options and portfolio management. |
| `maths-python` | Streamlit app with Python snippets for calculus, stochastic calculus, linear algebra and probability. |
| `quant-finance-flash-cards` | Flask flash-card app: 1,679 cards, 16 topics, linked by a concept graph. |
| `repo-tools` | New. Holds `sync_repos.py`, which checks all the repos for remote changes. |

The first three are Python applications managed with **uv**. `repo-tools` is a small set of scripts, also managed with uv.

---

## 2. What was done, in order

### 2.1 Launch scripts made portable

The Streamlit launch scripts ran `source .venv/bin/activate`. On Windows a virtual environment keeps its scripts in `.venv/Scripts/`, not `.venv/bin/`, so that line failed. All of them now just run `uv run streamlit run ...`, and `uv` finds and uses `.venv` itself on every operating system. Each script also changes into its own directory first and puts `~/.local/bin` on the PATH, so it works from anywhere. Later, every launcher was changed to forward extra arguments with `"$@"`, so `./launch_options_streamlit_app.sh --server.port 8599` works.

Windows cannot run a `.sh` file by itself. It needs a POSIX shell such as Git Bash (`bash script.sh`), WSL, Cygwin or MSYS2. A `.bat` or `.ps1` file runs natively.

### 2.2 uv installed and virtual environments built

`uv` was installed with `winget install astral-sh.uv`. `uv sync` was then run in the Python repos, creating a `.venv` in each from `pyproject.toml` and `uv.lock`.

### 2.3 Flash cards app fix

The app refused to start with `SECRET_KEY environment variable must be set in production`. The check is in `app/__init__.py`. It is skipped when `FLASK_DEBUG=1`. Both launch scripts now set that variable, and the README was corrected, because it claimed a random key was always generated.

`FLASK_DEBUG=1` only lifts the `SECRET_KEY` requirement. It does not turn on the Flask debugger or reloader. `run.py` decides that from a separate `DEBUG` variable, which defaults to false.

### 2.4 Best-practice improvements

| Area | Change |
|---|---|
| Lint | Ruff configured and pinned in `quant-finance-flash-cards`, with the same rule set as the sibling repos. 9 unused imports were fixed in flash-cards. |
| Pre-commit | `.pre-commit-config.yaml` added to flash-cards. |
| CI | New `tests.yml` in `maths-python` (with coverage). `quant-finance`'s test workflow now reports coverage. New `code-quality.yml` in flash-cards. `quant-finance`'s code-quality job now runs through uv. New or changed workflows have `permissions: contents: read` and cancel superseded runs. |
| Dependabot | `.github/dependabot.yml` added to the Python repos, covering uv packages and GitHub Actions. |
| Small fixes | `uv.lock` removed from `quant-finance/.gitignore` (it was ignored and tracked at once). `.coverage` added to the ignore files. |

### 2.5 Dependabot's first pull requests

Dependabot opened four PRs straight away:

- `appleboy/ssh-action` 1.0.3 to 1.2.5 in `maths-python`, `quant-finance` and `quant-finance-flash-cards`. These were merged one at a time, starting with flash-cards. Each deploy to EC2 and its health check passed.
- `antlr4-python3-runtime` 4.11 to 4.13.2 in `maths-python`. This was **not merged**. SymPy's `parse_latex` only works with antlr4 4.11, and 4.13.2 raises `ImportError`, which would crash the Lagrange page. CI had gone green because no test touched `parse_latex`. The PR was closed, Dependabot was told to ignore the package, and `tests/test_lagrange_latex.py` now fails if the pin is broken.

### 2.6 `sync_repos.py`

A script that checks all the repos for upstream changes and offers to pull. It lives in the new `repo-tools` repo. See [section 6](#6-the-sync_repospy-script).

---

## 3. Git concepts explained

### Repository, commit, branch

- A **repository** (repo) is a project folder plus its full history, stored in the hidden `.git` folder.
- A **commit** is a snapshot of the project with a message, an author, a date and a pointer to its parent commit. Each has a unique hash such as `29e51e6`.
- A **branch** is a movable label pointing at a commit. `main` is the default branch. New commits move the label forward.
- **HEAD** is the commit you currently have checked out, normally the tip of your current branch. **Detached HEAD** means HEAD points straight at a commit instead of a branch, so new commits belong to no branch and are easy to lose.

### Local, remote, origin, upstream

- A **remote** is another copy of the repo, usually on GitHub. **`origin`** is the default name for the one you cloned from.
- A **remote-tracking branch** such as `origin/main` is your local record of where `main` was on GitHub the last time you contacted it. It only changes when you fetch or pull.
- The **upstream** of a local branch is the remote branch it is linked to, usually `origin/main`. It is what `git pull` and `git push` default to, and `@{u}` is shorthand for it. A branch with no upstream has nothing to compare against, which is why `repo-tools` was reported as "no upstream" before it had a remote.

### Clone, fetch, pull, push

| Command | What it does |
|---|---|
| `git clone <url>` | Copies a remote repo to your machine and sets `origin` and the upstream. |
| `git fetch` | Downloads new commits from the remote and updates `origin/main`. Your own branch and working files do not change. Safe to run any time. |
| `git pull` | `git fetch` followed by merging (or rebasing) the new commits into your current branch. |
| `git push` | Uploads your local commits to the remote branch. |

`git fetch --prune` also deletes remote-tracking branches whose branch was deleted on the remote, for example after a merged PR.

### Ahead and behind

After a fetch, your branch and its upstream can differ:

- **Behind N**: the remote has N commits you do not have. This is what `sync_repos.py` looks for.
- **Ahead N**: you have N commits the remote does not.
- **Diverged**: both, so each side has commits the other lacks.
- **Up to date**: neither.

`git status -sb` shows this, for example `## main...origin/main [behind 1]`. Git counts it with `git rev-list --left-right --count HEAD...@{u}`.

### Fast-forward, merge and rebase

Suppose `main` has commits A-B and the remote has added C and D.

- **Fast-forward.** You have no commits of your own, so git simply moves your `main` label forward to D. No new commit is created and history stays a straight line. `git pull --ff-only` refuses to do anything else, so it errors instead of creating a merge or rewriting history. That is why it was used to bring the three repos up to date.
- **Merge** (`git pull`, the default). If both sides have new commits, git creates a **merge commit** with two parents, joining the lines. History is truthful but fills up with merge commits.
- **Rebase** (`git pull --rebase`). Git sets your local commits aside, moves to the remote's latest commit, then replays your commits on top of it. History stays a straight line. The replayed commits get **new hashes**, because their parent changed.

The golden rule of rebasing: never rebase commits that other people already have, because rewriting them forces everyone else to reconcile. Rebasing your own unpushed commits onto the remote's new work is exactly the safe case, and `sync_repos.py` warns you when you have unpushed commits.

For a solo repo that is only behind, `git pull`, `git pull --rebase` and `git pull --ff-only` all give the same result. `git config --global pull.rebase true` makes rebase the default.

### Conflicts and aborting

A **conflict** happens when your commit and the incoming one change the same lines and git cannot decide which wins. A rebase stops partway and waits for you to resolve it. `git rebase --abort` abandons the rebase and puts the repo back exactly as it was. `sync_repos.py` does this automatically, so it never leaves a repo stuck mid-rebase.

### Working tree, staging area, stash

- The **working tree** is the files on disk. The **staging area** (index) is what `git add` prepares for the next commit. A **dirty** tree has uncommitted changes.
- A rebase needs a clean tree. `git stash` shelves uncommitted changes temporarily. `sync_repos.py` skips dirty repos instead of stashing silently.

### Hooks

A **hook** is a script git runs at certain moments, such as just before a commit. A pre-commit hook can, for example, stop a commit if a generated file would drift from its source. A hook folder only applies after `git config core.hooksPath .githooks`, once per clone.

### Line endings (the CRLF warnings)

Windows ends lines with CRLF, Linux with LF. You saw "LF will be replaced by CRLF" because your Windows git is set to `core.autocrlf`. The repository stores LF and only your working copy changes, so the `.sh` scripts still run on Linux. Shell scripts with CRLF endings fail on Linux, so keep them as LF in the repo.

### Tracked and ignored files

`.gitignore` lists files git should never track, such as `.venv/`, `__pycache__/` and `.coverage`. Ignoring a file that is already tracked does nothing, which is why `uv.lock` listed in `quant-finance/.gitignore` was contradictory. Lockfiles for applications should be committed.

### Squash merge

When a PR is merged on GitHub, "squash" combines all its commits into one new commit on `main`. The Dependabot PRs were merged this way and their branches deleted.

### Co-authored commits

The commit messages end with a `Co-Authored-By:` line, a standard git trailer that GitHub shows as an additional author.

---

## 4. Python tooling concepts

- **uv** is a fast Python package and project manager. `uv sync` creates `.venv` from the project files. `uv run <command>` runs a command inside that environment, creating it first if needed. `uv add --dev <pkg>` adds a development dependency.
- **pyproject.toml** declares the project: name, `requires-python`, dependencies and tool settings. **`uv.lock`** records the exact version of every dependency so everyone gets the same ones. Commit it for applications.
- **Dependency groups.** `[dependency-groups] dev` holds tools used for development (pytest, ruff) rather than at runtime.
- **Virtual environment (`.venv`).** A private folder of packages for one project. Never commit it, and do not copy it between machines: the Windows one has `Scripts/` and Windows executables that do not work on Linux. Delete it and run `uv sync` on the new machine.
- **Ruff** is a fast linter and formatter. A **linter** finds likely bugs and style problems without running the code. The rule set is pinned (`select = ["E4","E7","E9","F"]`: syntax errors, undefined names, unused imports) because Ruff 0.17 widened its defaults and broke CI once.
- **Black** is an opinionated code formatter. **pre-commit** is a framework that runs tools like Black, Ruff and detect-secrets before each commit.
- **detect-secrets** scans for accidentally committed passwords and keys.
- **pytest** runs the tests. **Coverage** (`pytest-cov`) reports which lines the tests executed, for example 87% in `maths-python`.

---

## 5. CI and GitHub concepts

- **CI** (continuous integration) runs checks automatically on GitHub for every push and pull request. A **workflow** is one YAML file in `.github/workflows/`. A workflow has **jobs**, and each job has **steps**.
- **Triggers.** `on: push` runs when commits are pushed to `main`. `on: pull_request` runs when a PR is opened or updated.
- **`permissions: contents: read`** limits what the workflow's token can do, which is safer by default.
- **`concurrency` with `cancel-in-progress`** cancels an older run when a newer one starts for the same branch, saving time.
- **Actions** are reusable steps, such as `actions/checkout` (fetch the code) and `astral-sh/setup-uv`. They are pinned to versions like `@v7`. Pinning to a commit SHA is stricter, because tags can move.
- **Deploy.** The `deploy-ec2.yml` files build a Docker image, push it to GitHub's container registry, then connect to an EC2 server over SSH with `appleboy/ssh-action` and restart the app. The deploy job runs only on `main`, so a PR's CI cannot test an `ssh-action` bump. That is why the bumps were merged one repo at a time with the deploys watched.
- **Dependabot** is a GitHub service that opens PRs to update dependencies. It checks weekly, groups Python updates into one PR and never merges anything itself. `ignore:` entries tell it to skip a package.
- **A passing CI run is only as good as the tests.** The `antlr4` PR passed CI and would still have broken a page, because no test covered it. The new test closes that gap.
- **`gh`** is GitHub's command-line tool. It was used to list runs (`gh run list`), watch them (`gh run watch`), and merge or close PRs (`gh pr merge`, `gh pr close`).

---

## 6. The sync_repos.py script

It scans the repos next to it, fetches each one, shows the commits you are missing, and offers to `git pull --rebase`.

```bash
cd C:\Repos\repo-tools
uv run sync_repos.py              # fetch, report, then prompt per repo
uv run sync_repos.py --check      # report only; exit 1 if any repo is behind
uv run sync_repos.py --yes        # pull every eligible repo without asking
uv run sync_repos.py --root PATH  # scan PATH instead of the parent folder
uv run sync_repos.py --no-fetch   # compare against the last fetch
```

**Flow:** discover repos, then `git fetch --prune` in each in parallel, then report the commits you are behind by, then prompt `[y/N/a(ll)/q(uit)]`, then `git pull --rebase`, then print a summary.

**Safety:**

- Dirty repos are skipped and reported. Nothing is stashed.
- Unpushed local commits trigger a warning that they will be rebased.
- A conflicted rebase is aborted, leaving the repo unchanged, and the exit code is 1.
- No upstream, detached HEAD and failed fetches are listed under "Needs attention".
- Without a terminal it behaves like `--check`.

**Exit codes:** 0 means nothing is behind or everything approved was pulled. 1 means `--check` found a repo behind, or a pull failed or hit a conflict.

**Tests:** 19 tests build throwaway local repos for each case, so they need no network. They cover behind, diverged, conflict, dirty, no upstream, detached HEAD, fetch failure, the prompt answers and the exit codes.

The code is deliberately small functions (`discover_repos`, `inspect_repo`, `pull_rebase`, `render_report`, `render_summary`, `run`). The prompt and output functions are passed in, which is what makes it testable without a terminal.

---

## 7. Cheat sheet

```bash
git status -sb                  # branch, ahead/behind, changed files
git fetch --prune               # update remote-tracking branches, change nothing else
git log --oneline HEAD..@{u}    # commits on the remote you do not have
git pull --ff-only              # move forward only; error if histories diverged
git pull --rebase               # replay your commits on top of the remote's
git rebase --abort              # undo a rebase that hit conflicts
git push                        # upload your commits
git stash / git stash pop       # shelve and restore uncommitted changes
gh run list                     # recent GitHub Actions runs
gh run watch <id>               # follow a run until it finishes
gh pr list / gh pr merge <n>    # list and merge pull requests
uv sync                         # create or refresh .venv
uv run pytest                   # run tests in the project environment
```

**Setting up a second machine (for example Linux):** install `git`, `uv` and `gh`; clone the repos; run `uv sync` in each; run `git config core.hooksPath .githooks` in any repo that keeps its hooks there; set `FLASK_DEBUG=1` for the flash-cards app.
