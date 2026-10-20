import subprocess
from pathlib import Path

import pytest

import sync_repos
from sync_repos import (
    discover_repos,
    inspect_repo,
    pull_rebase,
    render_report,
    render_summary,
    run,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def world(tmp_path):
    """root/alpha is the local clone; other is a second clone used to push."""
    root = tmp_path / "root"
    root.mkdir()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    alpha = root / "alpha"
    git(tmp_path, "clone", "-q", str(remote), str(alpha))
    commit(alpha, "a.txt", "1\n", "initial")
    git(alpha, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    return root, alpha, other


def push_from_other(other: Path, filename="b.txt", message="remote change"):
    commit(other, filename, "x\n", message)
    git(other, "push", "-q", "origin", "main")


def collect():
    lines: list[str] = []
    return lines, lines.append


def test_discover_repos_finds_only_git_dirs(world):
    root, alpha, _ = world
    (root / "not-a-repo").mkdir()
    assert discover_repos(root) == [alpha]


def test_up_to_date(world):
    _, alpha, _ = world
    state = inspect_repo(alpha)
    assert (state.ahead, state.behind, state.problem) == (0, 0, None)
    assert "Up to date: alpha" in render_report([state])


def test_behind_lists_incoming_commits(world):
    _, alpha, other = world
    push_from_other(other, message="add b")
    state = inspect_repo(alpha)
    assert state.behind == 1
    assert state.commits[0].subject == "add b"
    assert "1 file changed" in state.shortstat
    report = render_report([state])
    assert "alpha  [main]  1 commit behind origin/main" in report
    assert "Tester: add b" in report


def test_no_fetch_does_not_see_new_commits(world):
    _, alpha, other = world
    push_from_other(other)
    assert inspect_repo(alpha, fetch=False).behind == 0


def test_pull_rebase_updates_and_summarises(world):
    _, alpha, other = world
    push_from_other(other)
    outcome = pull_rebase(inspect_repo(alpha))
    assert outcome.result == "updated" and outcome.commits == 1
    assert (alpha / "b.txt").exists()
    assert "updated" in render_summary([outcome])


def test_dependency_change_hints_uv_sync(world):
    _, alpha, other = world
    push_from_other(other, filename="uv.lock", message="bump")
    outcome = pull_rebase(inspect_repo(alpha))
    assert outcome.run_uv_sync
    assert "run 'uv sync' in alpha" in render_summary([outcome])


def test_diverged_rebase_keeps_local_commit(world):
    _, alpha, other = world
    commit(alpha, "local.txt", "l\n", "local work")
    push_from_other(other)
    state = inspect_repo(alpha)
    assert state.diverged and "diverged" in render_report([state])
    assert pull_rebase(state).result == "updated"
    assert git(alpha, "log", "--format=%s", "-2").splitlines()[0] == "local work"


def test_conflict_is_aborted_and_repo_left_clean(world):
    _, alpha, other = world
    commit(alpha, "a.txt", "local\n", "local edit")
    commit(other, "a.txt", "remote\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    before = git(alpha, "rev-parse", "HEAD")
    outcome = pull_rebase(inspect_repo(alpha))
    assert outcome.result == "conflict"
    assert git(alpha, "rev-parse", "HEAD") == before
    assert git(alpha, "status", "--porcelain") == ""
    assert not sync_repos.rebase_in_progress(alpha)


def test_no_upstream_and_detached_are_reported(world):
    _, alpha, _ = world
    git(alpha, "checkout", "-q", "-b", "scratch")
    assert "no upstream" in inspect_repo(alpha).problem
    git(alpha, "checkout", "-q", "--detach")
    assert inspect_repo(alpha).problem == "detached HEAD"


def test_fetch_failure_is_reported_not_raised(world):
    _, alpha, _ = world
    git(alpha, "remote", "set-url", "origin", str(alpha.parent / "missing.git"))
    state = inspect_repo(alpha)
    assert state.fetch_error and "fetch failed" in state.problem


def test_check_mode_exit_codes_and_no_changes(world):
    root, alpha, other = world
    _, out = collect()
    assert run(root, check=True, out=out) == 0
    push_from_other(other)
    assert run(root, check=True, out=out) == 1
    assert not (alpha / "b.txt").exists()


@pytest.mark.parametrize(
    "answer, pulled", [("y", True), ("n", False), ("", False), ("q", False)]
)
def test_prompt_answers(world, answer, pulled):
    root, alpha, other = world
    push_from_other(other)
    _, out = collect()
    run(root, prompt=lambda _: answer, out=out)
    assert (alpha / "b.txt").exists() is pulled


def test_answer_all_pulls_every_repo(world, tmp_path):
    root, alpha, other = world
    beta = root / "beta"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(beta))
    push_from_other(other)
    answers = iter(["a"])
    run(root, prompt=lambda _: next(answers), out=lambda _: None)
    assert (alpha / "b.txt").exists() and (beta / "b.txt").exists()


def test_yes_flag_pulls_without_prompting(world):
    root, alpha, other = world
    push_from_other(other)

    def fail(_):
        raise AssertionError("should not prompt")

    assert run(root, assume_yes=True, prompt=fail, out=lambda _: None) == 0
    assert (alpha / "b.txt").exists()


def test_dirty_repo_is_skipped(world):
    root, alpha, other = world
    push_from_other(other)
    (alpha / "a.txt").write_text("dirty\n")
    lines, out = collect()
    run(root, assume_yes=True, out=out)
    assert not (alpha / "b.txt").exists()
    assert "skipped" in "".join(lines)


def test_conflict_makes_run_exit_nonzero(world):
    root, alpha, other = world
    commit(alpha, "a.txt", "local\n", "local edit")
    commit(other, "a.txt", "remote\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    assert run(root, assume_yes=True, out=lambda _: None) == 1
