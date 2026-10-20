"""Check sibling git repos for upstream changes and offer to `git pull --rebase`.

Scans the parent directory of this repo (or --root) for git repos, fetches each
one, summarises the commits it is missing, asks before pulling, then summarises
what happened.  Standard library only, so `uv run sync_repos.py` just works.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
FIELD_SEP = "\x1f"


@dataclass
class Commit:
    sha: str
    date: str
    author: str
    subject: str


@dataclass
class RepoState:
    path: Path
    branch: str | None = None  # None means detached HEAD
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    fetch_error: str | None = None
    commits: list[Commit] = field(default_factory=list)
    shortstat: str = ""
    touches_dependencies: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0

    @property
    def problem(self) -> str | None:
        """Why this repo cannot be compared with its upstream, if it cannot."""
        if self.fetch_error:
            return f"fetch failed: {self.fetch_error}"
        if self.branch is None:
            return "detached HEAD"
        if self.upstream is None:
            return f"branch '{self.branch}' has no upstream"
        return None


@dataclass
class Outcome:
    name: str
    result: str  # updated | declined | skipped | conflict | failed
    detail: str = ""
    old: str = ""
    new: str = ""
    commits: int = 0
    shortstat: str = ""
    run_uv_sync: bool = False


def run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `path`, never prompting for credentials."""
    return subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def discover_repos(root: Path) -> list[Path]:
    """Return the child directories of `root` that are git repos."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def inspect_repo(path: Path, fetch: bool = True) -> RepoState:
    """Fetch (optionally) and describe how `path` relates to its upstream."""
    state = RepoState(path=path)
    if fetch:
        fetched = run_git(path, "fetch", "--prune")
        if fetched.returncode != 0:
            lines = fetched.stderr.strip().splitlines()
            state.fetch_error = lines[-1] if lines else "unknown error"

    head = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    state.branch = head.stdout.strip() if head.returncode == 0 else None

    upstream = run_git(
        path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    state.upstream = upstream.stdout.strip() if upstream.returncode == 0 else None

    state.dirty = bool(
        run_git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    )

    if state.upstream is None:
        return state

    counts = run_git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts.returncode == 0:
        ahead, behind = counts.stdout.split()
        state.ahead, state.behind = int(ahead), int(behind)

    if state.behind:
        log = run_git(
            path,
            "log",
            f"--format=%h{FIELD_SEP}%as{FIELD_SEP}%an{FIELD_SEP}%s",
            "HEAD..@{u}",
        )
        state.commits = [
            Commit(*line.split(FIELD_SEP, 3))
            for line in log.stdout.splitlines()
            if line
        ]
        state.shortstat = run_git(
            path, "diff", "--shortstat", "HEAD...@{u}"
        ).stdout.strip()
        changed = run_git(path, "diff", "--name-only", "HEAD...@{u}").stdout
        state.touches_dependencies = any(
            Path(name).name in DEPENDENCY_FILES for name in changed.splitlines()
        )
    return state


def rebase_in_progress(path: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        git_path = run_git(path, "rev-parse", "--git-path", name).stdout.strip()
        if (path / git_path).exists():
            return True
    return False


def pull_rebase(state: RepoState) -> Outcome:
    """`git pull --rebase`; a conflicted rebase is aborted so the repo is left clean."""
    path = state.path
    old = run_git(path, "rev-parse", "--short", "HEAD").stdout.strip()
    pulled = run_git(path, "pull", "--rebase")
    if pulled.returncode != 0:
        if rebase_in_progress(path):
            run_git(path, "rebase", "--abort")
            return Outcome(
                state.name,
                "conflict",
                "rebase hit conflicts and was aborted; the repo is unchanged",
                old=old,
                new=old,
            )
        lines = (pulled.stderr or pulled.stdout).strip().splitlines()
        return Outcome(
            state.name, "failed", lines[-1] if lines else "git pull failed", old=old
        )
    new = run_git(path, "rev-parse", "--short", "HEAD").stdout.strip()
    return Outcome(
        state.name,
        "updated",
        old=old,
        new=new,
        commits=state.behind,
        shortstat=state.shortstat,
        run_uv_sync=state.touches_dependencies,
    )


def render_report(states: list[RepoState], color: bool = False) -> str:
    """Describe every repo: incoming commits first, then anything needing attention."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines: list[str] = []
    for state in (s for s in states if s.behind and not s.problem):
        plural = "s" if state.behind != 1 else ""
        flags = []
        if state.diverged:
            flags.append(f"diverged, {state.ahead} local commit(s) not pushed")
        if state.dirty:
            flags.append("uncommitted changes")
        suffix = f"  ({'; '.join(flags)})" if flags else ""
        lines.append(
            paint(
                f"{state.name}  [{state.branch}]  {state.behind} commit{plural} "
                f"behind {state.upstream}{suffix}",
                "1",
            )
        )
        for commit in state.commits:
            lines.append(
                f"  {commit.sha}  {commit.date}  {commit.author}: {commit.subject}"
            )
        if state.shortstat:
            lines.append(f"  {state.shortstat}")
        lines.append("")

    attention = [(s, s.problem) for s in states if s.problem]
    if attention:
        lines.append(paint("Needs attention:", "33"))
        lines.extend(f"  {s.name}: {reason}" for s, reason in attention)
        lines.append("")

    current = [s.name for s in states if not s.problem and not s.behind]
    if current:
        lines.append(f"Up to date: {', '.join(current)}")
    return "\n".join(lines).rstrip() + "\n"


def render_summary(outcomes: list[Outcome]) -> str:
    """Summarise what the pull step did to each repo."""
    if not outcomes:
        return "Nothing was pulled.\n"
    lines = ["Summary:"]
    for o in outcomes:
        if o.result == "updated":
            plural = "s" if o.commits != 1 else ""
            stat = f"; {o.shortstat}" if o.shortstat else ""
            lines.append(
                f"  {o.name}: updated {o.old} -> {o.new}, "
                f"{o.commits} commit{plural} pulled{stat}"
            )
            if o.run_uv_sync:
                lines.append(
                    f"    dependencies changed: run 'uv sync' in {o.name}"
                )
        else:
            detail = f" ({o.detail})" if o.detail else ""
            lines.append(f"  {o.name}: {o.result}{detail}")
    updated = sum(o.result == "updated" for o in outcomes)
    problems = sum(o.result in ("conflict", "failed") for o in outcomes)
    lines.append(f"{updated} updated, {problems} with problems.")
    return "\n".join(lines) + "\n"


def decide(
    state: RepoState,
    prompt: Callable[[str], str],
    assume_yes: bool,
    pull_all: bool,
) -> tuple[str, bool]:
    """Return ('yes' | 'no' | 'quit', pull_all) for one repo."""
    if assume_yes or pull_all:
        return "yes", pull_all
    warning = (
        f" ({state.ahead} unpushed local commit(s) will be rebased)"
        if state.diverged
        else ""
    )
    answer = (
        prompt(f"Pull --rebase into {state.name}{warning}? [y/N/a(ll)/q(uit)] ")
        .strip()
        .lower()
    )
    if answer in ("y", "yes"):
        return "yes", False
    if answer in ("a", "all"):
        return "yes", True
    if answer in ("q", "quit"):
        return "quit", False
    return "no", False


def run(
    root: Path,
    *,
    check: bool = False,
    assume_yes: bool = False,
    fetch: bool = True,
    color: bool = False,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = lambda text: print(text, end=""),
) -> int:
    repos = discover_repos(root)
    if not repos:
        out(f"No git repos found in {root}\n")
        return 0

    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        states = list(pool.map(lambda p: inspect_repo(p, fetch), repos))

    out(render_report(states, color))
    behind = [s for s in states if s.behind and not s.problem]
    if check:
        return 1 if behind else 0

    outcomes: list[Outcome] = []
    pull_all = False
    for state in behind:
        if state.dirty:
            outcomes.append(
                Outcome(state.name, "skipped", "uncommitted changes; commit or stash")
            )
            continue
        choice, pull_all = decide(state, prompt, assume_yes, pull_all)
        if choice == "quit":
            break
        if choice == "no":
            outcomes.append(Outcome(state.name, "declined"))
            continue
        outcomes.append(pull_rebase(state))

    if outcomes:
        out("\n" + render_summary(outcomes))
    return 1 if any(o.result in ("conflict", "failed") for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="directory containing the repos (default: parent of this repo)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only, never prompt; exit 1 if any repo is behind",
    )
    parser.add_argument(
        "--yes", action="store_true", help="pull every eligible repo without asking"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="compare against the last fetch"
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return run(
        args.root,
        check=args.check or not (interactive or args.yes),
        assume_yes=args.yes,
        fetch=not args.no_fetch,
        color=interactive and "NO_COLOR" not in os.environ,
    )


if __name__ == "__main__":
    sys.exit(main())
