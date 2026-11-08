"""Show detailed git status for sibling git repos, without changing any of them.

Scans the parent directory of this repo (or --root) for git repos and reports,
for each one, the branch, how far it is ahead of or behind its upstream, the
staged, unstaged, untracked and conflicted files, stashes, any operation left
half-finished, and the last commit.  Standard library only, so
`uv run repo_status.py` just works.

Reuses the git plumbing from sync_repos.py.  Needs git >= 2.21 (`%as` date format).
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sync_repos import (
    FIELD_SEP,
    Commit,
    RepoError,
    add_root_argument,
    discover_repos,
    fetch_prune,
    git_out,
    in_progress_operation,
    run_git,
)

DEFAULT_MAX_FILES = 10

BOLD = "1"
YELLOW = "33"


@dataclass(slots=True)
class RepoStatus:
    path: Path
    branch: str | None = None  # None means detached HEAD
    upstream: str | None = None
    upstream_gone: bool = False  # configured, but deleted on the remote
    ahead: int = 0
    behind: int = 0
    staged: list[str] = field(default_factory=list)  # "M  path" style entries
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    stashes: int = 0
    operation: str | None = None  # a rebase/merge/... already in progress
    last_commit: Commit | None = None  # None for a repo with no commits yet
    fetch_error: str | None = None
    error: str | None = None  # a git command failed; state is unreliable

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked or self.conflicted)

    @property
    def clean(self) -> bool:
        """Nothing to commit, push, pull, or finish.  Stashes are informational."""
        return not (self.dirty or self.ahead or self.behind or self.operation)

    @property
    def problem(self) -> str | None:
        """Why this report may be wrong or missing, if it may be."""
        if self.error:
            return self.error
        if self.fetch_error:
            return f"fetch failed: {self.fetch_error}"
        return None


def _parse_header(status: RepoStatus, header: str) -> None:
    """Apply one `# branch.*` line of `git status --porcelain=v2 --branch`."""
    key, _, value = header.partition(" ")
    match key:
        case "branch.head":
            status.branch = None if value == "(detached)" else value
        case "branch.upstream":
            # git omits branch.ab when the upstream branch no longer exists.
            status.upstream, status.upstream_gone = value, True
        case "branch.ab":
            ahead, behind = value.split()  # e.g. "+2 -1"
            status.ahead, status.behind = abs(int(ahead)), abs(int(behind))
            status.upstream_gone = False


def _add_entry(status: RepoStatus, xy: str, path: str) -> None:
    """File a changed path under staged and/or unstaged; '.' means unchanged."""
    staged, unstaged = xy
    if staged != ".":
        status.staged.append(f"{staged}  {path}")
    if unstaged != ".":
        status.unstaged.append(f"{unstaged}  {path}")


def parse_porcelain(status: RepoStatus, output: str) -> None:
    """Fill `status` from `git status --porcelain=v2 --branch -z` output.

    Raises ValueError for output that does not look like porcelain v2, so a
    git change cannot silently produce an all-clear.
    """
    records = iter(output.split("\0"))
    for record in records:
        kind, _, rest = record.partition(" ")
        match kind:
            case "#":
                _parse_header(status, rest)
            case "1":  # XY sub mH mI mW hH hI path
                _add_entry(status, rest[:2], rest.split(" ", 7)[-1])
            case "2":  # XY sub mH mI mW hH hI Xscore path, then the original path
                path = rest.split(" ", 8)[-1]
                original = next(records, None)
                if not original:
                    raise ValueError(f"truncated rename record for {path!r}")
                _add_entry(status, rest[:2], f"{original} -> {path}")
            case "u":  # XY sub m1 m2 m3 mW h1 h2 h3 path
                status.conflicted.append(rest.split(" ", 9)[-1])
            case "?":
                status.untracked.append(rest)
            case "" | "!":  # trailing NUL, or an ignored file
                pass
            case _:
                raise ValueError(f"unexpected status record: {record!r}")


def _inspect(status: RepoStatus, fetch: bool) -> None:
    path = status.path
    if fetch:
        status.fetch_error = fetch_prune(path)

    # -z so paths with spaces or odd characters arrive unquoted.
    parse_porcelain(status, git_out(path, "status", "--porcelain=v2", "--branch", "-z"))
    status.operation = in_progress_operation(path)
    status.stashes = len(git_out(path, "stash", "list").splitlines())

    # A repo with no commits yet makes `git log` fail; that is not an error.
    log = run_git(
        path, "log", "-1", f"--format=%h{FIELD_SEP}%as{FIELD_SEP}%an{FIELD_SEP}%s"
    )
    if log.returncode == 0 and log.stdout.strip():
        status.last_commit = Commit.parse(log.stdout.strip())


def inspect_repo(path: Path, fetch: bool = False) -> RepoStatus:
    """Describe the state of `path`.

    Never raises for a misbehaving repo: the failure is recorded on the status
    so one bad repo cannot hide the report for the others.
    """
    status = RepoStatus(path=path)
    try:
        _inspect(status, fetch)
    except (OSError, subprocess.SubprocessError, RepoError, ValueError) as exc:
        status.error = str(exc)
    return status


def _relation(status: RepoStatus) -> str:
    """Say how the branch compares with its upstream, in a few words."""
    if status.upstream is None:
        return "no upstream"
    if status.upstream_gone:
        return f"{status.upstream} (gone)"
    parts = [
        f"{label} {count}"
        for label, count in (("ahead", status.ahead), ("behind", status.behind))
        if count
    ]
    if not parts:
        return f"in sync with {status.upstream}"
    return f"{', '.join(parts)} of {status.upstream}"


def _clean_label(status: RepoStatus) -> str:
    """Name a clean repo, noting what "clean" cannot vouch for."""
    notes = []
    if status.branch is None:
        notes.append("detached HEAD")
    elif status.upstream is None:
        notes.append("no upstream")
    elif status.upstream_gone:
        notes.append("upstream gone")
    if status.stashes:
        notes.append(f"{status.stashes} stashed")
    return f"{status.name} ({', '.join(notes)})" if notes else status.name


def _section(title: str, entries: list[str], max_files: int) -> list[str]:
    """List `entries` under `title`, capped at `max_files` (0 means no cap)."""
    if not entries:
        return []
    shown = entries[:max_files] if max_files else entries
    lines = [f"  {title} ({len(entries)}):"]
    lines.extend(f"    {entry}" for entry in shown)
    if len(shown) < len(entries):
        lines.append(f"    ... {len(entries) - len(shown)} more")
    return lines


def _render_repo(
    status: RepoStatus, max_files: int, paint: Callable[[str, str], str]
) -> list[str]:
    branch = status.branch or "detached HEAD"
    lines = [paint(f"{status.name}  [{branch}]  {_relation(status)}", BOLD)]
    if status.operation:
        lines.append(paint(f"  {status.operation} in progress", YELLOW))
    lines += _section("conflicted", status.conflicted, max_files)
    lines += _section("staged", status.staged, max_files)
    lines += _section("unstaged", status.unstaged, max_files)
    lines += _section("untracked", status.untracked, max_files)
    if status.stashes:
        lines.append(f"  stashes: {status.stashes}")
    if commit := status.last_commit:
        lines.append(
            f"  last commit: {commit.sha}  {commit.date}  "
            f"{commit.author}: {commit.subject}"
        )
    return [*lines, ""]


def render_report(
    statuses: list[RepoStatus],
    color: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
) -> str:
    """Describe every repo with something to do, then problems, then clean repos."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines: list[str] = []
    for status in statuses:
        if not status.error and not status.clean:
            lines += _render_repo(status, max_files, paint)

    problems = [(s, s.problem) for s in statuses if s.problem]
    if problems:
        lines.append(paint("Needs attention:", YELLOW))
        lines.extend(f"  {s.name}: {reason}" for s, reason in problems)
        lines.append("")

    clean = [_clean_label(s) for s in statuses if s.clean and not s.problem]
    if clean:
        lines.append(f"Clean: {', '.join(clean)}")
    return "\n".join(lines).rstrip() + "\n"


def status_all(
    root: Path,
    *,
    fetch: bool = False,
    color: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    out: Callable[[str], None] = lambda text: print(text, end=""),
) -> int:
    """Run the whole report; returns the process exit code."""
    repos = discover_repos(root)
    if not repos:
        out(f"No git repos found in {root}\n")
        return 0

    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        statuses = list(pool.map(lambda p: inspect_repo(p, fetch), repos))

    out(render_report(statuses, color, max_files))
    if any(s.problem for s in statuses):
        return 2
    return 0 if all(s.clean for s in statuses) else 1


def non_negative_int(text: str) -> int:
    """argparse type: reject negative counts instead of misreading them."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show detailed git status for sibling git repos."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fetch each repo first so ahead/behind is current "
        "(default: use the last fetch)",
    )
    parser.add_argument(
        "--max-files",
        type=non_negative_int,
        default=DEFAULT_MAX_FILES,
        help="files listed per category per repo, 0 for all "
        f"(default: {DEFAULT_MAX_FILES})",
    )
    args = parser.parse_args(argv)

    if shutil.which("git") is None:
        print("repo_status: git was not found on PATH", file=sys.stderr)
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        return status_all(
            args.root,
            fetch=args.fetch,
            # NO_COLOR applies only when set to a non-empty value.
            color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
            max_files=args.max_files,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
