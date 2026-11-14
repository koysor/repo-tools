import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from clone_repos import RemoteRepo, clone_missing, remote_repos, uses_ssh
from review_prs import GhError


def git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_remote(tmp_path: Path, name: str, pyproject: bool = False) -> Path:
    """A bare repo with one commit, standing in for a GitHub repo."""
    work = tmp_path / "work" / name
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text(f"# {name}\n")
    if pyproject:
        (work / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(exist_ok=True)
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


class FakeGh:
    """Stands in for gh: answers `gh repo list` with the given repos."""

    def __init__(self, repos: list[dict] | None = None, returncode: int = 0):
        self.repos = repos or []
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.returncode:
            return subprocess.CompletedProcess(
                args, self.returncode, "", "not logged in"
            )
        return subprocess.CompletedProcess(args, 0, json.dumps(self.repos), "")


def entry(name: str, url: str, ssh_url: str = "") -> dict:
    return {
        "name": name,
        "url": url,
        "sshUrl": ssh_url or f"git@github.com:me/{name}.git",
    }


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "repos"
    path.mkdir()
    return path


def run(root, gh, *, check=False, assume_yes=False, answer="y", ssh=False):
    lines: list[str] = []
    asked: list[str] = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    code = clone_missing(
        root,
        "me",
        check=check,
        assume_yes=assume_yes,
        ssh=ssh,
        gh=gh,
        ask=ask,
        out=lines.append,
    )
    return code, lines, asked


def test_remote_repos_asks_for_sources_only_and_sorts():
    gh = FakeGh(
        [entry("zeta", "https://github.com/me/zeta"), entry("Alpha", "https://a")]
    )
    repos = remote_repos("me", gh, ssh=False)
    assert [r.name for r in repos] == ["Alpha", "zeta"]
    assert repos[1].clone_url == "https://github.com/me/zeta"
    args = gh.calls[0]
    assert args[:3] == ["repo", "list", "me"]
    assert "--no-archived" in args and "--source" in args


def test_remote_repos_uses_ssh_urls_when_asked():
    gh = FakeGh([entry("zeta", "https://github.com/me/zeta")])
    assert remote_repos("me", gh, ssh=True) == [
        RemoteRepo("zeta", "git@github.com:me/zeta.git")
    ]


def test_gh_failure_raises():
    with pytest.raises(GhError):
        remote_repos("me", FakeGh(returncode=1), ssh=False)


def test_nothing_missing(root, tmp_path):
    git(root, "init", "-q", str(root / "app"))
    code, lines, asked = run(root, FakeGh([entry("app", "unused")]))
    assert code == 0
    assert lines == [f"All 1 GitHub repos of me are cloned in {root}"]
    assert asked == []


def test_check_reports_missing_without_cloning(root, tmp_path):
    bare = make_remote(tmp_path, "app")
    code, lines, asked = run(root, FakeGh([entry("app", str(bare))]), check=True)
    assert code == 1
    assert lines == [f"Not cloned in {root}: app"]
    assert asked == []
    assert not (root / "app").exists()


def test_clones_after_yes(root, tmp_path):
    app = make_remote(tmp_path, "app", pyproject=True)
    notes = make_remote(tmp_path, "notes")
    git(root, "init", "-q", str(root / "existing"))
    gh = FakeGh(
        [
            entry("app", str(app)),
            entry("existing", "unused"),
            entry("notes", str(notes)),
        ]
    )
    code, lines, asked = run(root, gh)
    assert code == 0
    assert asked == ["Clone 2 repo(s)? [y/N] "]
    assert lines == [
        f"Not cloned in {root}: app, notes",
        "app: cloned",
        "notes: cloned",
        "Run `uv sync` in: app",
    ]
    assert (root / "notes" / "README.md").read_text() == "# notes\n"
    assert git(root / "app", "remote", "get-url", "origin") == str(app)


def test_declined_clones_nothing(root, tmp_path):
    bare = make_remote(tmp_path, "app")
    code, lines, _ = run(root, FakeGh([entry("app", str(bare))]), answer="n")
    assert code == 1
    assert lines[-1] == "Nothing cloned."
    assert not (root / "app").exists()


def test_assume_yes_skips_the_prompt(root, tmp_path):
    bare = make_remote(tmp_path, "app")
    code, _, asked = run(root, FakeGh([entry("app", str(bare))]), assume_yes=True)
    assert code == 0
    assert asked == []
    assert (root / "app" / ".git").exists()


def test_failed_clone_is_reported(root, tmp_path):
    good = make_remote(tmp_path, "good")
    gh = FakeGh([entry("bad", str(tmp_path / "nowhere.git")), entry("good", str(good))])
    code, lines, _ = run(root, gh, assume_yes=True)
    assert code == 1
    assert lines[1].startswith("bad: clone failed: ")
    assert lines[2] == "good: cloned"


def test_non_repo_folder_is_left_alone(root, tmp_path):
    bare = make_remote(tmp_path, "app")
    (root / "app").mkdir()
    (root / "app" / "keep.txt").write_text("mine")
    code, lines, asked = run(root, FakeGh([entry("app", str(bare))]), assume_yes=True)
    assert code == 1
    assert lines == [f"app: {root / 'app'} exists but is not a git repo; left alone"]
    assert asked == []
    assert sorted(p.name for p in (root / "app").iterdir()) == ["keep.txt"]


@pytest.mark.parametrize(
    ("url", "ssh"),
    [
        ("git@github.com:me/repo-tools.git", True),
        ("ssh://git@github.com/me/repo-tools.git", True),
        ("https://github.com/me/repo-tools.git", False),
        (None, False),
    ],
)
def test_uses_ssh(tmp_path, url, ssh):
    git(tmp_path, "init", "-q")
    if url:
        git(tmp_path, "remote", "add", "origin", url)
    assert uses_ssh(tmp_path) is ssh
