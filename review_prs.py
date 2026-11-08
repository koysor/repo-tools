"""Review open pull requests across sibling repos and merge the ones you approve.

Scans the parent directory of this repo (or --root) for git repos whose origin
is on GitHub, lists each one's open pull requests with the `gh` CLI, summarises
them (author, size, files, CI checks, mergeability, review state), recommends
merge / review / blocked, asks per pull request, then squash-merges the
approved ones.  Standard library only, so `uv run review_prs.py` just works.

Reuses the git plumbing from sync_repos.py.  Needs the GitHub CLI (`gh`),
logged in with `gh auth login`.
"""

import argparse
import enum
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sync_repos import DEPENDENCY_FILES, Choice, RepoError, discover_repos, run_git

GH_TIMEOUT = 120
DEFAULT_MAX_FILES = 10
UNKNOWN_RETRIES = 2  # extra fetches while GitHub is still computing `mergeable`
RETRY_DELAY = 2.0

PR_FIELDS = ",".join(
    (
        "number",
        "title",
        "url",
        "author",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
        "statusCheckRollup",
        "additions",
        "deletions",
        "changedFiles",
        "files",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "createdAt",
    )
)

GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
    r"(?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)
MERGE_REF = re.compile(r"^(?P<repo>[^#@\s]+)#(?P<number>\d+)@(?P<sha>[0-9a-fA-F]{40})$")

GREEN = "32"
YELLOW = "33"
RED = "31"
BOLD = "1"

GhRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class GhError(Exception):
    """A gh command we depend on failed, so the pull request state is unknown."""


class Verdict(enum.StrEnum):
    MERGE = "merge"  # recommended: safe to merge
    REVIEW = "review"  # offered, but look at it first
    BLOCKED = "blocked"  # never offered


class Result(enum.StrEnum):
    MERGED = "merged"
    DECLINED = "declined"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Checks:
    passed: int = 0
    skipped: int = 0
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.skipped + len(self.pending) + len(self.failed)

    def describe(self) -> str:
        if not self.total:
            return "no checks"
        parts = []
        if self.passed:
            parts.append(f"{self.passed} passed")
        if self.failed:
            parts.append(f"{len(self.failed)} failed ({', '.join(self.failed)})")
        if self.pending:
            parts.append(f"{len(self.pending)} pending ({', '.join(self.pending)})")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)


def parse_checks(rollup: list[dict[str, Any]] | None) -> Checks:
    """Count a statusCheckRollup, which mixes CheckRun and StatusContext items."""
    passed = skipped = 0
    pending: list[str] = []
    failed: list[str] = []
    for item in rollup or []:
        name = item.get("name") or item.get("context") or "unnamed check"
        if item.get("__typename") == "StatusContext" or (
            "state" in item and "conclusion" not in item
        ):
            state = (item.get("state") or "").upper()
            if state == "SUCCESS":
                passed += 1
            elif state in ("", "PENDING", "EXPECTED"):
                pending.append(name)
            else:
                failed.append(name)
            continue
        if (item.get("status") or "").upper() != "COMPLETED":
            pending.append(name)
            continue
        conclusion = (item.get("conclusion") or "").upper()
        if conclusion == "SUCCESS":
            passed += 1
        elif conclusion in ("SKIPPED", "NEUTRAL"):
            skipped += 1
        else:  # FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED, STALE, ...
            failed.append(name)
    return Checks(passed, skipped, pending, failed)


@dataclass(slots=True)
class PullRequest:
    repo: str  # local folder name
    slug: str  # owner/name on GitHub
    number: int
    title: str
    url: str
    author: str
    author_is_bot: bool
    draft: bool
    mergeable: str  # MERGEABLE, CONFLICTING or UNKNOWN
    merge_state: str  # mergeStateStatus: CLEAN, BEHIND, BLOCKED, DIRTY, ...
    review_decision: str  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED or ""
    checks: Checks
    additions: int
    deletions: int
    changed_files: int
    files: list[str]
    head_ref: str
    head_sha: str
    base_ref: str
    created_at: str

    @classmethod
    def from_gh(cls, repo: str, slug: str, data: dict[str, Any]) -> PullRequest:
        author = data.get("author") or {}
        return cls(
            repo=repo,
            slug=slug,
            number=int(data["number"]),
            title=data.get("title") or "",
            url=data.get("url") or "",
            author=author.get("login") or "ghost",
            author_is_bot=bool(author.get("is_bot")),
            draft=bool(data.get("isDraft")),
            mergeable=(data.get("mergeable") or "UNKNOWN").upper(),
            merge_state=(data.get("mergeStateStatus") or "").upper(),
            review_decision=(data.get("reviewDecision") or "").upper(),
            checks=parse_checks(data.get("statusCheckRollup")),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files=int(data.get("changedFiles") or 0),
            files=[f["path"] for f in data.get("files") or [] if f.get("path")],
            head_ref=data.get("headRefName") or "",
            head_sha=data.get("headRefOid") or "",
            base_ref=data.get("baseRefName") or "",
            created_at=data.get("createdAt") or "",
        )

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def merge_arg(self) -> str:
        """The `--merge` argument that merges exactly this head commit."""
        return f"{self.ref}@{self.head_sha}"


@dataclass(frozen=True, slots=True)
class Assessment:
    verdict: Verdict
    reasons: tuple[str, ...]

    @property
    def offered(self) -> bool:
        return self.verdict is not Verdict.BLOCKED


def assess(pr: PullRequest) -> Assessment:
    """Recommend merge, review or blocked from the PR's state alone."""
    blocked = []
    if pr.draft:
        blocked.append("draft")
    if pr.mergeable == "CONFLICTING" or pr.merge_state == "DIRTY":
        blocked.append(f"conflicts with {pr.base_ref or 'the base branch'}")
    if pr.checks.failed:
        blocked.append(f"checks failed: {', '.join(pr.checks.failed)}")
    if pr.review_decision == "CHANGES_REQUESTED":
        blocked.append("changes requested in review")
    if blocked:
        return Assessment(Verdict.BLOCKED, tuple(blocked))

    review = []
    if pr.checks.pending:
        review.append(f"checks still running: {', '.join(pr.checks.pending)}")
    if not pr.checks.total:
        review.append("no CI checks ran")
    if pr.mergeable != "MERGEABLE":
        review.append("GitHub has not yet worked out whether it merges cleanly")
    if pr.merge_state == "BEHIND":
        review.append(f"branch is behind {pr.base_ref}; GitHub may refuse the merge")
    elif pr.merge_state == "BLOCKED":
        review.append("branch protection requirements are not met yet")
    if not pr.author_is_bot:
        review.append(f"opened by {pr.author}, not a bot")
        if any(Path(name).name in DEPENDENCY_FILES for name in pr.files):
            review.append("changes dependencies")
    if review:
        return Assessment(Verdict.REVIEW, tuple(review))

    return Assessment(
        Verdict.MERGE,
        (f"bot PR from {pr.author}", "all checks passed", "merges cleanly"),
    )


@dataclass(slots=True)
class RepoPRs:
    path: Path
    slug: str | None = None
    prs: list[PullRequest] = field(default_factory=list)
    error: str | None = None  # the repo could not be inspected

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(slots=True)
class Outcome:
    ref: str
    result: Result
    detail: str = ""
    url: str = ""


def run_gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run gh without any way to prompt the user."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["gh", *args],  # noqa: S607 - gh is looked up on PATH on purpose
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=GH_TIMEOUT,
        env={
            **os.environ,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
        },
    )


def _last_line(done: subprocess.CompletedProcess[str]) -> str:
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit status {done.returncode}"


def gh_json(gh: GhRunner, args: Sequence[str]) -> Any:
    """Parsed stdout of a gh command that must succeed; GhError otherwise."""
    try:
        done = gh(args)
    except subprocess.TimeoutExpired:
        raise GhError(f"gh {args[0]} timed out after {GH_TIMEOUT}s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhError(f"gh {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed: {_last_line(done)}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh {' '.join(args[:2])} returned invalid JSON: {exc}") from exc


def github_slug(url: str) -> str | None:
    """owner/name for a GitHub remote URL (https or ssh), else None."""
    match = GITHUB_REMOTE.match(url.strip())
    return match["slug"] if match else None


def repo_slug(path: Path) -> str:
    """owner/name of the GitHub repo that `path`'s origin points at."""
    try:
        done = run_git(path, "remote", "get-url", "origin")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoError(f"git remote failed: {exc}") from exc
    if done.returncode != 0:
        raise RepoError("no 'origin' remote")
    url = done.stdout.strip()
    slug = github_slug(url)
    if slug is None:
        raise RepoError(f"origin is not on GitHub: {url}")
    return slug


def fetch_pr(repo: str, slug: str, number: int, gh: GhRunner) -> PullRequest:
    data = gh_json(gh, ["pr", "view", str(number), "-R", slug, "--json", PR_FIELDS])
    return PullRequest.from_gh(repo, slug, data)


def settle_mergeable(pr: PullRequest, gh: GhRunner) -> PullRequest:
    """GitHub computes `mergeable` lazily; ask again while it says UNKNOWN."""
    for _ in range(UNKNOWN_RETRIES):
        if pr.mergeable != "UNKNOWN":
            break
        time.sleep(RETRY_DELAY)
        pr = fetch_pr(pr.repo, pr.slug, pr.number, gh)
    return pr


def list_prs(path: Path, gh: GhRunner) -> RepoPRs:
    """Open pull requests of one repo.  Never raises for a misbehaving repo."""
    found = RepoPRs(path=path)
    try:
        found.slug = slug = repo_slug(path)
        data = gh_json(
            gh,
            [
                "pr",
                "list",
                "-R",
                slug,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                PR_FIELDS,
            ],
        )
        prs = [PullRequest.from_gh(found.name, slug, item) for item in data]
        found.prs = [settle_mergeable(pr, gh) for pr in prs]
    except (RepoError, GhError, KeyError, TypeError, ValueError) as exc:
        found.error = str(exc)
    return found


def merge_pr(pr: PullRequest, gh: GhRunner, head_sha: str | None = None) -> Outcome:
    """Squash-merge `pr`, but only if its head is still the reviewed commit.

    -R keeps gh away from any local clone: it deletes the remote branch only
    and never checks out or pulls anything locally.
    """
    args = [
        "pr",
        "merge",
        str(pr.number),
        "-R",
        pr.slug,
        "--squash",
        "--delete-branch",
        "--match-head-commit",
        head_sha or pr.head_sha,
    ]
    try:
        done = gh(args)
    except subprocess.TimeoutExpired:
        return Outcome(pr.ref, Result.FAILED, f"gh timed out after {GH_TIMEOUT}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Outcome(pr.ref, Result.FAILED, str(exc))
    if done.returncode != 0:
        return Outcome(pr.ref, Result.FAILED, _last_line(done), pr.url)
    return Outcome(pr.ref, Result.MERGED, "squash-merged, branch deleted", pr.url)


def age(created_at: str, now: datetime) -> str:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return "?"
    seconds = max(0, int((now - created).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def render_report(
    repos: list[RepoPRs],
    color: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    now: datetime | None = None,
) -> str:
    """Describe every open PR, then repos needing attention, then quiet repos."""
    now = now or datetime.now(UTC)

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    verdict_colour = {Verdict.MERGE: GREEN, Verdict.REVIEW: YELLOW}
    lines: list[str] = []
    for repo in repos:
        for pr in repo.prs:
            assessment = assess(pr)
            plural = "s" if pr.changed_files != 1 else ""
            lines.append(paint(f"{pr.ref}  {pr.title}", BOLD))
            lines.append(
                f"  {pr.author} | {pr.head_ref} -> {pr.base_ref} | "
                f"+{pr.additions}/-{pr.deletions} in {pr.changed_files} file{plural} | "
                f"opened {age(pr.created_at, now)}"
            )
            lines.append(f"  checks: {pr.checks.describe()}")
            shown = pr.files if max_files <= 0 else pr.files[:max_files]
            lines.extend(f"    {name}" for name in shown)
            if len(shown) < len(pr.files):
                lines.append(f"    ... and {len(pr.files) - len(shown)} more")
            lines.append(
                paint(
                    f"  -> {assessment.verdict}: {'; '.join(assessment.reasons)}",
                    verdict_colour.get(assessment.verdict, RED),
                )
            )
            lines.append(f"  {pr.url}")
            lines.append("")

    attention = [r for r in repos if r.error]
    if attention:
        lines.append(paint("Needs attention:", YELLOW))
        lines.extend(f"  {r.name}: {r.error}" for r in attention)
        lines.append("")

    quiet = [r.name for r in repos if not r.error and not r.prs]
    if quiet:
        lines.append(f"No open pull requests: {', '.join(quiet)}")
    if not lines:
        return "No repos to review.\n"
    return "\n".join(lines).rstrip() + "\n"


def render_json(repos: list[RepoPRs]) -> str:
    """Machine-readable report, for the review-prs skill and other tools."""
    payload = []
    for repo in repos:
        prs = []
        for pr in repo.prs:
            assessment = assess(pr)
            item = asdict(pr)
            item["checks"]["total"] = pr.checks.total
            item.update(
                ref=pr.ref,
                merge_arg=pr.merge_arg,
                verdict=str(assessment.verdict),
                reasons=list(assessment.reasons),
            )
            prs.append(item)
        payload.append(
            {
                "name": repo.name,
                "slug": repo.slug,
                "error": repo.error,
                "prs": prs,
            }
        )
    return json.dumps({"repos": payload}, indent=2) + "\n"


def render_summary(outcomes: list[Outcome]) -> str:
    if not outcomes:
        return "Nothing was merged.\n"
    lines = ["Summary:"]
    for o in outcomes:
        detail = f" ({o.detail})" if o.detail else ""
        lines.append(f"  {o.ref}: {o.result}{detail}")
    merged = [o for o in outcomes if o.result is Result.MERGED]
    failed = sum(o.result is Result.FAILED for o in outcomes)
    lines.append(f"{len(merged)} merged, {failed} failed.")
    if merged:
        repos = sorted({o.ref.split("#")[0] for o in merged})
        lines.append(
            f"Local clones are now behind ({', '.join(repos)}); pull with "
            "'uv run --project repo-tools repo-tools/sync_repos.py'."
        )
    return "\n".join(lines) + "\n"


def decide(
    pr: PullRequest,
    assessment: Assessment,
    prompt: Callable[[str], str],
    merge_all: bool,
) -> tuple[Choice, bool]:
    """Return the choice for one PR and whether to stop asking (merge_all).

    "all" covers the remaining PRs recommended for merge only; PRs flagged for
    review are always asked about one by one.
    """
    if merge_all and assessment.verdict is Verdict.MERGE:
        return Choice.YES, True
    flag = " - needs review" if assessment.verdict is Verdict.REVIEW else ""
    try:
        answer = prompt(f"Squash-merge {pr.ref}{flag}? [y/N/a(ll)/q(uit)] ")
    except EOFError:
        return Choice.QUIT, merge_all
    answer = answer.strip().lower()
    if answer in ("y", "yes"):
        return Choice.YES, merge_all
    if answer in ("a", "all"):
        return Choice.YES, True
    if answer in ("q", "quit"):
        return Choice.QUIT, merge_all
    return Choice.NO, merge_all


def merge_requested(
    root: Path, refs: Sequence[str], gh: GhRunner, out: Callable[[str], None]
) -> int:
    """Merge exactly the PRs named as REPO#NUMBER@HEAD_SHA, re-checking each."""
    repos = {p.name: p for p in discover_repos(root)}
    outcomes: list[Outcome] = []
    for ref in refs:
        match = MERGE_REF.match(ref)
        if not match:
            outcomes.append(
                Outcome(ref, Result.SKIPPED, "expected REPO#NUMBER@<40-char head sha>")
            )
            continue
        name, number, sha = match["repo"], int(match["number"]), match["sha"].lower()
        label = f"{name}#{number}"
        if name not in repos:
            outcomes.append(
                Outcome(label, Result.SKIPPED, f"no repo '{name}' in {root}")
            )
            continue
        try:
            pr = settle_mergeable(
                fetch_pr(name, repo_slug(repos[name]), number, gh), gh
            )
        except (RepoError, GhError, KeyError, TypeError, ValueError) as exc:
            outcomes.append(Outcome(label, Result.FAILED, str(exc)))
            continue
        if pr.head_sha.lower() != sha:
            outcomes.append(
                Outcome(label, Result.SKIPPED, "new commits since review; review again")
            )
            continue
        assessment = assess(pr)
        if not assessment.offered:
            outcomes.append(
                Outcome(label, Result.SKIPPED, "; ".join(assessment.reasons), pr.url)
            )
            continue
        outcomes.append(merge_pr(pr, gh, sha))
    out(render_summary(outcomes))
    return 0 if all(o.result is Result.MERGED for o in outcomes) else 1


def review(
    root: Path,
    *,
    check: bool = False,
    assume_yes: bool = False,
    as_json: bool = False,
    color: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    gh: GhRunner = run_gh,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = lambda text: print(text, end=""),
    now: datetime | None = None,
) -> int:
    """Run the whole workflow; returns the process exit code."""
    repo_paths = discover_repos(root)
    if not repo_paths:
        out(
            json.dumps({"repos": []}) + "\n"
            if as_json
            else f"No git repos found in {root}\n"
        )
        return 0

    with ThreadPoolExecutor(max_workers=min(8, len(repo_paths))) as pool:
        repos = list(pool.map(lambda p: list_prs(p, gh), repo_paths))

    prs = [pr for repo in repos for pr in repo.prs]
    if as_json or check:
        out(
            render_json(repos)
            if as_json
            else render_report(repos, color, max_files, now)
        )
        if any(r.error for r in repos):
            return 2
        return 1 if prs else 0

    out(render_report(repos, color, max_files, now))
    outcomes: list[Outcome] = []
    merge_all = False
    interrupted = False
    try:
        for pr in prs:
            assessment = assess(pr)
            if not assessment.offered:
                outcomes.append(
                    Outcome(pr.ref, Result.SKIPPED, "; ".join(assessment.reasons))
                )
                continue
            if assume_yes:
                if assessment.verdict is not Verdict.MERGE:
                    outcomes.append(
                        Outcome(
                            pr.ref,
                            Result.DECLINED,
                            "needs review; --yes merges only recommended PRs",
                        )
                    )
                    continue
                choice = Choice.YES
            else:
                choice, merge_all = decide(pr, assessment, prompt, merge_all)
            if choice is Choice.QUIT:
                break
            if choice is Choice.NO:
                outcomes.append(Outcome(pr.ref, Result.DECLINED))
                continue
            outcomes.append(merge_pr(pr, gh))
    except KeyboardInterrupt:
        interrupted = True
        out("\nInterrupted.\n")

    if outcomes:
        out("\n" + render_summary(outcomes))
    if interrupted:
        return 130
    return 1 if any(o.result is Result.FAILED for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review open pull requests in sibling repos and merge approved ones."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="directory containing the repos (default: parent of this repo)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="report only, never merge; exit 1 if any PR is open, "
        "2 if any repo could not be inspected",
    )
    mode.add_argument(
        "--yes",
        action="store_true",
        help="merge every PR recommended for merge without asking",
    )
    mode.add_argument(
        "--json", action="store_true", help="print the report as JSON; never merge"
    )
    mode.add_argument(
        "--merge",
        nargs="+",
        metavar="REPO#N@SHA",
        help="merge exactly these PRs, if their head is still SHA and they are "
        "not blocked (SHA is the merge_arg from --json)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"changed files to list per PR; 0 for all (default {DEFAULT_MAX_FILES})",
    )
    args = parser.parse_args(argv)

    for tool in ("git", "gh"):
        if shutil.which(tool) is None:
            print(f"review_prs: {tool} was not found on PATH", file=sys.stderr)
            return 2
    try:
        auth = run_gh(["auth", "status", "--hostname", "github.com"])
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"review_prs: gh auth status failed: {exc}", file=sys.stderr)
        return 2
    if auth.returncode != 0:
        print(
            "review_prs: gh is not logged in to github.com; run 'gh auth login'",
            file=sys.stderr,
        )
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.merge:
            return merge_requested(
                args.root, args.merge, run_gh, lambda text: print(text, end="")
            )
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        return review(
            args.root,
            check=args.check or not (interactive or args.yes or args.json),
            assume_yes=args.yes,
            as_json=args.json,
            # NO_COLOR applies only when set to a non-empty value.
            color=interactive and not args.json and not os.environ.get("NO_COLOR"),
            max_files=args.max_files,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
