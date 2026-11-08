# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Layout

The repos folder (`C:\Repos` on Windows) is not a git repository or a monorepo. It is a plain folder of independent sibling repos, each with its own remote (`github.com/koysor/<name>`), virtual environment and tooling. This file is kept in git as `repo-tools/workspace/CLAUDE.md`; the `CLAUDE.md` in the repos folder is a copy written by `repo-tools/install_workspace.py` (which `sync-check.ps1` runs). Edit the git copy and re-run the script, never the copy. `sync-check.*` are not under version control.

Run git, `uv` and test commands from inside the relevant repo, or point at it with `git -C <repo>` / `uv run --project <repo>`. There is no root-level `pyproject.toml`, so `uv run` from the root has no project to use. A change that spans repos needs a separate commit in each.

| Repo | What it is | Project guidance |
|---|---|---|
| `cqf` | Certificate in Quantitative Finance coursework: notebooks, lecture notes, exams | `cqf/CLAUDE.md` |
| `maths-python` | Streamlit app of maths and statistics snippets, with `src/maths_lib` | `maths-python/CLAUDE.md` |
| `quant-finance` | Four Streamlit apps on top of the `src/quant_finance` library | `quant-finance/CLAUDE.md` |
| `quant-finance-flash-cards` | Flask app serving Markdown flash cards as a knowledge graph | `quant-finance-flash-cards/CLAUDE.md` |
| `repo-tools` | Scripts for checking and syncing all the sibling repos | `repo-tools/README.md` |
| `dataframe-compare` | Placeholder, no code yet | `README.md` |
| `football-scores` | Placeholder, no code yet; will predict match scores from Betfair odds | `README.md` |

Follow the guidance file of the repo you are working in; it takes precedence over this file. The placeholder READMEs say to start with `uv init` when work begins.

## Shared conventions

- All Python projects use `uv` and Python 3.14. `cqf` is pinned to exactly 3.14.8; the others use `>=3.14`.
- Use `uv run <tool>` rather than calling `python`, `pytest` or `ruff` directly. Code in these repos can use 3.14-only syntax that older interpreters reject (for example, unparenthesised `except A, B:`).
- Claude skills used in more than one repo live in `repo-tools/skills/` and are shared through the `koysor-skills` plugin, which `repo-tools/install_workspace.py` installs at user scope (see `repo-tools/README.md`). Skills specific to one repo stay in that repo's `.claude/skills/<name>/SKILL.md`.
- On Windows, several repos' guidance and scripts assume a POSIX shell and macOS or Linux tools: the `launch_*.sh` scripts, `source .venv/bin/activate`, `lsof`. Run those through the Bash tool (Git Bash), or use the direct `uv run ...` equivalent; `lsof` needs a Windows substitute.

## Cross-repo status and sync

```powershell
.\sync-check.cmd        # fetch, show status of every repo, then prompt to sync
.\sync-check.ps1 -Yes   # same, without the prompt
```

`sync-check.ps1` wraps two scripts from `repo-tools`. To run them directly:

```bash
uv run --project repo-tools repo-tools/repo_status.py --fetch --root .   # read-only report
uv run --project repo-tools repo-tools/sync_repos.py --check --root .    # report only; exit 1 if any repo is behind
```

`repo_status.py` exit codes are a report rather than a failure: 0 means clean, 1 means something needs committing, pushing, pulling or finishing, and 2 means a repo could not be inspected.

Open pull requests across all repos (needs `gh`, logged in):

```bash
uv run --project repo-tools repo-tools/review_prs.py --check --root .   # summarise every open PR with a merge/review/blocked verdict
uv run --project repo-tools repo-tools/review_prs.py --root .           # same, then prompt per PR and squash-merge the approved ones
```

The `koysor-skills:review-prs` skill adds a Claude review of each diff on top and merges only what the user selects, via `--merge REPO#N@SHA`. After merging, the local clones are behind; pull with `sync_repos.py`.

`sync_repos.py` rebases onto incoming commits rather than merging. Without `--check` it can rewrite local history, and `sync-check.ps1 -Yes` skips only the first prompt (`sync_repos.py` still asks per repo). Both scripts discover repos by scanning the parent folder, so a new sibling repo is picked up automatically.

## Developing repo-tools

`repo-tools` has no `CLAUDE.md` of its own. Run these from inside `repo-tools`:

```bash
uv sync
uv run pytest
uv run pytest tests/test_sync_repos.py                 # one file
uv run pytest tests/test_repo_status.py::test_clean_repo   # one test
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

- `repo_status.py`, `review_prs.py` and `install_workspace.py` import their git plumbing or `--root` handling from `sync_repos.py`, so the files must stay together.
- `review_prs.py` tests fake `gh` with a callable, so they need no network or GitHub login either.
- Tests build throwaway local bare remotes and clones, so they need no network.
- The ruff rule set is pinned deliberately in `pyproject.toml` because ruff is unpinned in CI. `sync_repos.py` carries targeted `noqa` comments for the subprocess rules (S603, S607) instead of a blanket ignore.
