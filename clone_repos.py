"""Clone the GitHub repos that are missing from the repos folder.

Lists the owner's repos on GitHub with the `gh` CLI (archived repos and forks
left out), compares them with the git repos in the parent directory of this
repo (or --root), shows which are missing, asks, then clones them.  The owner
is the one this repo's `origin` points at, unless --owner is given.  Clones use
the same protocol (ssh or https) as this repo's origin.

Never touches a repo that is already there.  A folder with a missing repo's
name that is not a git repo is reported and left alone.  Standard library only,
so `uv run clone_repos.py` just works.  Needs `gh`, logged in with
`gh auth login`.
"""

import argparse
import io
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from review_prs import GhError, GhRunner, gh_json, repo_slug, run_gh
from sync_repos import RepoError, add_root_argument, discover_repos, run_git

CLONE_TIMEOUT = 600
REPO_LIMIT = 1000
THIS_REPO = Path(__file__).resolve().parent


@dataclass
class RemoteRepo:
    name: str
    clone_url: str


def uses_ssh(path: Path) -> bool:
    """Whether `path`'s origin is an ssh URL rather than https."""
    done = run_git(path, "remote", "get-url", "origin")
    url = done.stdout.strip() if done.returncode == 0 else ""
    return url.startswith(("git@", "ssh://"))


def remote_repos(owner: str, gh: GhRunner, ssh: bool) -> list[RemoteRepo]:
    """The owner's non-archived, non-fork GitHub repos, sorted by name."""
    data = gh_json(
        gh,
        [
            "repo",
            "list",
            owner,
            "--limit",
            str(REPO_LIMIT),
            "--no-archived",
            "--source",
            "--json",
            "name,url,sshUrl",
        ],
    )
    repos = [
        RemoteRepo(item["name"], item["sshUrl"] if ssh else item["url"])
        for item in data
    ]
    return sorted(repos, key=lambda r: r.name.lower())


def clone(root: Path, repo: RemoteRepo) -> str | None:
    """Clone `repo` into root/<name>; None on success, else the reason."""
    try:
        done = run_git(
            root, "clone", "--", repo.clone_url, repo.name, timeout=CLONE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {CLONE_TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        return lines[-1] if lines else f"exit status {done.returncode}"
    return None


def clone_missing(
    root: Path,
    owner: str,
    *,
    check: bool,
    assume_yes: bool,
    ssh: bool,
    gh: GhRunner = run_gh,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """List, report and clone; returns the process exit code."""
    repos = remote_repos(owner, gh, ssh)
    local = {p.name for p in discover_repos(root)}
    missing = [r for r in repos if r.name not in local]
    blocked = [r for r in missing if (root / r.name).exists()]
    to_clone = [r for r in missing if r not in blocked]

    for repo in blocked:
        out(f"{repo.name}: {root / repo.name} exists but is not a git repo; left alone")
    if not to_clone:
        if not blocked:
            out(f"All {len(repos)} GitHub repos of {owner} are cloned in {root}")
        return 1 if blocked else 0

    out(f"Not cloned in {root}: {', '.join(r.name for r in to_clone)}")
    if check:
        return 1
    if not assume_yes:
        try:
            answer = ask(f"Clone {len(to_clone)} repo(s)? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out("Nothing cloned.")
            return 1

    failed = False
    needs_sync = []
    for repo in to_clone:
        reason = clone(root, repo)
        if reason is None:
            out(f"{repo.name}: cloned")
            if (root / repo.name / "pyproject.toml").is_file():
                needs_sync.append(repo.name)
        else:
            out(f"{repo.name}: clone failed: {reason}")
            failed = True
    if needs_sync:
        out(f"Run `uv sync` in: {', '.join(needs_sync)}")
    return 1 if failed or blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clone the owner's GitHub repos that are missing from the repos folder."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--owner",
        help="GitHub user or organisation (default: the owner of this repo's origin)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only, never clone; exit 1 if any repo is missing",
    )
    parser.add_argument(
        "--yes", action="store_true", help="clone every missing repo without asking"
    )
    args = parser.parse_args(argv)

    for tool in ("git", "gh"):
        if shutil.which(tool) is None:
            print(f"clone_repos: {tool} was not found on PATH", file=sys.stderr)
            return 2
    if not args.root.is_dir():
        print(f"clone_repos: {args.root} is not a folder", file=sys.stderr)
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        owner = args.owner or repo_slug(THIS_REPO).split("/")[0]
        return clone_missing(
            args.root,
            owner,
            check=args.check or not (interactive or args.yes),
            assume_yes=args.yes,
            ssh=uses_ssh(THIS_REPO),
        )
    except (GhError, RepoError) as exc:
        print(f"clone_repos: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
