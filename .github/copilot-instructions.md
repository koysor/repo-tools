# Copilot instructions

## Commands

The command-line tools use only the Python standard library at runtime;
development dependencies are managed with `uv` (Python 3.14).

```bash
uv sync
uv run repo_status.py
uv run sync_repos.py --check
uv run pytest
uv run pytest tests/test_sync_repos.py
uv run pytest tests/test_sync_repos.py::test_name
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Use the corresponding test module or pytest node ID for focused tests. The
tests create temporary local repositories and bare remotes, so they do not
need network access.

## Architecture and conventions

`repo_status.py` reports branch/upstream state and working-tree changes without
changing repositories. `sync_repos.py` shares its Git plumbing and handles
fetch/report/optional rebase. Keep shared discovery and subprocess helpers in
`sync_repos.py`; keep report rendering and decision logic separable from Git
operations so tests can inject output and prompt functions.

`repo_status.py` scans sibling repositories from the parent directory by
default; `sync_repos.py` does the same but offers rebases only after reporting
incoming commits. `--check` is report-only. Rebase against the exact upstream
commit shown to the user, skip repositories with uncommitted changes, and abort
a conflicted rebase rather than leaving a sibling repository mid-operation.
Avoid destructive side effects in the status-reporting path.

Both tools use structured dataclasses for parsed repository state, bounded
Git subprocess calls with prompts disabled, and explicit error reporting.
Tests isolate user Git configuration and construct throwaway repositories;
keep these tests independent of the actual sibling repositories and remote
network.
