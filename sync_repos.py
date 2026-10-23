"""Check sibling git repos for upstream changes and offer to rebase onto them.

Scans the parent directory of this repo (or --root) for git repos, fetches each
one, summarises the commits it is missing, asks before rebasing, then summarises
what happened.  Standard library only, so `uv run sync_repos.py` just works.

Needs git >= 2.21 (`%as` date format).
"""

import argparse
import enum
import io
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
FIELD_SEP = "\x1f"
GIT_TIMEOUT = 30
FETCH_TIMEOUT = 60
REBASE_TIMEOUT = 300

# Directories/files under .git that mean another operation owns the repo.
OPERATION_MARKERS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)


class RepoError(Exception):
    """A git command we depend on failed, so the repo's state is unknown."""


class Result(enum.StrEnum):
    UPDATED = "updated"
    DECLINED = "declined"
    SKIPPED = "skipped"
    CONFLICT = "conflict"
    FAILED = "failed"


class Choice(enum.Enum):
    YES = enum.auto()
    NO = enum.auto()
    QUIT = enum.auto()


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    date: str
    author: str
    subject: str

    @classmethod
    def parse(cls, line: str) -> Commit:
        """Parse a `%h<sep>%as<sep>%an<sep>%s` line.

        git_out strips its output and str.strip() treats the separator as
        whitespace, so a commit with an empty subject arrives one field short.
        """
        fields = line.split(FIELD_SEP, 3)
        fields += [""] * (4 - len(fields))
        return cls(*fields)


@dataclass(slots=True)
class RepoState:
    path: Path
    branch: str | None = None  # None means detached HEAD
    upstream: str | None = None
    head_sha: str = ""
    upstream_sha: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    operation: str | None = None  # a rebase/merge/... already in progress
    fetch_error: str | None = None
    error: str | None = None  # a git command failed; state is unreliable
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
        if self.error:
            return self.error
        if self.fetch_error:
            return f"fetch failed: {self.fetch_error}"
        if self.operation:
            return f"{self.operation} in progress"
        if self.branch is None:
            return "detached HEAD"
        if self.upstream is None:
            return f"branch '{self.branch}' has no upstream"
        return None


@dataclass(slots=True)
class Outcome:
    name: str
    result: Result
    detail: str = ""
    old: str | None = None
    new: str | None = None
    commits: int = 0
    shortstat: str = ""
    run_uv_sync: bool = False


def run_git(
    path: Path, *args: str, timeout: float | None = GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run git in `path` without any way to prompt the user.

    stdin is closed, GIT_TERMINAL_PROMPT is off, and the child gets its own
    session so it has no controlling terminal: ssh passphrase and host-key
    prompts fail instead of fighting the interactive prompt for the tty.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607 - git is looked up on PATH on purpose
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def git_out(path: Path, *args: str) -> str:
    """Stdout of a git command that must succeed; RepoError otherwise."""
    done = run_git(path, *args)
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        reason = lines[-1] if lines else f"exit status {done.returncode}"
        raise RepoError(f"git {args[0]} failed: {reason}")
    return done.stdout.strip()


def discover_repos(root: Path) -> list[Path]:
    """Return the child directories of `root` that are git repos."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def in_progress_operation(path: Path) -> str | None:
    """Name the rebase/merge/cherry-pick/... that is mid-flight in `path`."""
    git_dir = Path(git_out(path, "rev-parse", "--absolute-git-dir"))
    for marker, operation in OPERATION_MARKERS:
        if (git_dir / marker).exists():
            return operation
    return None


def fetch_prune(path: Path) -> str | None:
    """Fetch `path` from its remotes; return why that failed, or None on success.

    --prune rewrites remote-tracking refs, even for read-only callers.
    """
    try:
        fetched = run_git(path, "fetch", "--prune", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"timed out after {FETCH_TIMEOUT}s"
    if fetched.returncode == 0:
        return None
    lines = fetched.stderr.strip().splitlines()
    return lines[-1] if lines else "unknown error"


def _inspect(state: RepoState, fetch: bool) -> None:
    path = state.path
    if fetch:
        state.fetch_error = fetch_prune(path)

    # symbolic-ref -q exits 1 for a detached HEAD but 128 for real failures.
    head = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    if head.returncode == 0:
        state.branch = head.stdout.strip()
    elif head.returncode != 1:
        raise RepoError(f"git symbolic-ref failed: {head.stderr.strip()}")

    state.operation = in_progress_operation(path)
    state.dirty = bool(git_out(path, "status", "--porcelain", "--untracked-files=no"))

    if state.branch is None or state.operation:
        return
    upstream = run_git(
        path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    if upstream.returncode != 0:
        return
    state.upstream = upstream.stdout.strip()

    state.head_sha, state.upstream_sha = git_out(
        path, "rev-parse", "HEAD", "@{u}"
    ).split()
    ahead, behind = git_out(
        path, "rev-list", "--left-right", "--count", "HEAD...@{u}"
    ).split()
    state.ahead, state.behind = int(ahead), int(behind)

    if state.behind:
        log = git_out(
            path,
            "log",
            f"--format=%h{FIELD_SEP}%as{FIELD_SEP}%an{FIELD_SEP}%s",
            "HEAD..@{u}",
        )
        state.commits = [Commit.parse(line) for line in log.splitlines() if line]
        state.shortstat = git_out(path, "diff", "--shortstat", "HEAD...@{u}")
        changed = git_out(path, "diff", "--name-only", "-z", "HEAD...@{u}")
        state.touches_dependencies = any(
            Path(name).name in DEPENDENCY_FILES for name in changed.split("\0")
        )


def inspect_repo(path: Path, fetch: bool = True) -> RepoState:
    """Fetch (optionally) and describe how `path` relates to its upstream.

    Never raises for a misbehaving repo: the failure is recorded on the state
    so one bad repo cannot hide the report for the others.
    """
    state = RepoState(path=path)
    try:
        _inspect(state, fetch)
    except (OSError, subprocess.SubprocessError, RepoError, ValueError) as exc:
        state.error = str(exc)
    return state


def pull_rebase(state: RepoState) -> Outcome:
    """Rebase onto the upstream commit that was reported, not whatever is newest.

    Rebasing onto the recorded sha needs no network and ignores pull.* config,
    so the user gets exactly the commits they were shown.  A conflicted rebase
    is aborted so the repo is left clean.
    """
    path = state.path
    try:
        if operation := in_progress_operation(path):
            return Outcome(state.name, Result.SKIPPED, f"{operation} in progress")
        old = git_out(path, "rev-parse", "HEAD")
        if old != state.head_sha:
            return Outcome(
                state.name, Result.SKIPPED, "HEAD moved since the report; run again"
            )
        short_old = git_out(path, "rev-parse", "--short", "HEAD")
        # --rebase-merges keeps local merge commits instead of flattening them.
        rebased = run_git(
            path,
            "rebase",
            "--rebase-merges",
            state.upstream_sha,
            timeout=REBASE_TIMEOUT,
        )
        if rebased.returncode != 0:
            if in_progress_operation(path) == "rebase":
                run_git(path, "rebase", "--abort")
                return Outcome(
                    state.name,
                    Result.CONFLICT,
                    "rebase hit conflicts and was aborted; the repo is unchanged",
                    old=short_old,
                    new=short_old,
                )
            lines = (rebased.stderr or rebased.stdout).strip().splitlines()
            return Outcome(
                state.name,
                Result.FAILED,
                lines[-1] if lines else "git rebase failed",
                old=short_old,
            )
        new = git_out(path, "rev-parse", "--short", "HEAD")
    except (OSError, subprocess.SubprocessError, RepoError) as exc:
        _abort_rebase_quietly(path)
        return Outcome(state.name, Result.FAILED, str(exc))
    return Outcome(
        state.name,
        Result.UPDATED,
        old=short_old,
        new=new,
        commits=state.behind,
        shortstat=state.shortstat,
        run_uv_sync=state.touches_dependencies,
    )


def _abort_rebase_quietly(path: Path) -> None:
    """After an unexpected error, leave no half-finished rebase that we started."""
    try:
        if in_progress_operation(path) == "rebase":
            run_git(path, "rebase", "--abort")
    except OSError, subprocess.SubprocessError, RepoError:
        pass


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
        lines.extend(
            f"  {c.sha}  {c.date}  {c.author}: {c.subject}" for c in state.commits
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
    """Summarise what the rebase step did to each repo."""
    if not outcomes:
        return "Nothing was pulled.\n"
    lines = ["Summary:"]
    for o in outcomes:
        if o.result is Result.UPDATED:
            plural = "s" if o.commits != 1 else ""
            stat = f"; {o.shortstat}" if o.shortstat else ""
            lines.append(
                f"  {o.name}: updated {o.old} -> {o.new}, "
                f"{o.commits} commit{plural} pulled{stat}"
            )
            if o.run_uv_sync:
                lines.append(f"    dependencies changed: run 'uv sync' in {o.name}")
        else:
            detail = f" ({o.detail})" if o.detail else ""
            lines.append(f"  {o.name}: {o.result}{detail}")
    updated = sum(o.result is Result.UPDATED for o in outcomes)
    problems = sum(o.result in (Result.CONFLICT, Result.FAILED) for o in outcomes)
    lines.append(f"{updated} updated, {problems} with problems.")
    return "\n".join(lines) + "\n"


def decide(
    state: RepoState,
    prompt: Callable[[str], str],
    assume_yes: bool,
    pull_all: bool,
) -> tuple[Choice, bool]:
    """Return the choice for one repo and whether to stop asking (pull_all)."""
    if assume_yes or pull_all:
        return Choice.YES, pull_all
    warning = (
        f" ({state.ahead} unpushed local commit(s) will be rebased)"
        if state.diverged
        else ""
    )
    try:
        answer = prompt(
            f"Rebase {state.name} onto {state.upstream}{warning}? [y/N/a(ll)/q(uit)] "
        )
    except EOFError:
        return Choice.QUIT, False
    answer = answer.strip().lower()
    if answer in ("y", "yes"):
        return Choice.YES, False
    if answer in ("a", "all"):
        return Choice.YES, True
    if answer in ("q", "quit"):
        return Choice.QUIT, False
    return Choice.NO, False


def sync(
    root: Path,
    *,
    check: bool = False,
    assume_yes: bool = False,
    fetch: bool = True,
    color: bool = False,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = lambda text: print(text, end=""),
) -> int:
    """Run the whole workflow; returns the process exit code."""
    repos = discover_repos(root)
    if not repos:
        out(f"No git repos found in {root}\n")
        return 0

    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        states = list(pool.map(lambda p: inspect_repo(p, fetch), repos))

    out(render_report(states, color))
    behind = [s for s in states if s.behind and not s.problem]
    if check:
        if any(s.problem for s in states):
            return 2
        return 1 if behind else 0

    outcomes: list[Outcome] = []
    pull_all = False
    interrupted = False
    try:
        for state in behind:
            if state.dirty:
                outcomes.append(
                    Outcome(
                        state.name,
                        Result.SKIPPED,
                        "uncommitted changes; commit or stash",
                    )
                )
                continue
            choice, pull_all = decide(state, prompt, assume_yes, pull_all)
            if choice is Choice.QUIT:
                break
            if choice is Choice.NO:
                outcomes.append(Outcome(state.name, Result.DECLINED))
                continue
            outcomes.append(pull_rebase(state))
    except KeyboardInterrupt:
        interrupted = True
        out("\nInterrupted.\n")

    if outcomes:
        out("\n" + render_summary(outcomes))
    if interrupted:
        return 130
    failed = any(o.result in (Result.CONFLICT, Result.FAILED) for o in outcomes)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check sibling git repos for upstream changes and offer to rebase."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="directory containing the repos (default: parent of this repo)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only, never prompt; exit 1 if any repo is behind, "
        "2 if any repo could not be checked",
    )
    parser.add_argument(
        "--yes", action="store_true", help="rebase every eligible repo without asking"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="compare against the last fetch"
    )
    args = parser.parse_args(argv)

    if shutil.which("git") is None:
        print("sync_repos: git was not found on PATH", file=sys.stderr)
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        return sync(
            args.root,
            check=args.check or not (interactive or args.yes),
            assume_yes=args.yes,
            fetch=not args.no_fetch,
            # NO_COLOR applies only when set to a non-empty value.
            color=interactive and not os.environ.get("NO_COLOR"),
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
