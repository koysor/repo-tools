import argparse
import subprocess
from pathlib import Path

import pytest

from repo_status import (
    RepoStatus,
    inspect_repo,
    main,
    non_negative_int,
    parse_porcelain,
    render_report,
    status_all,
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


@pytest.fixture
def repo(tmp_path):
    """root/alpha tracks a bare remote and has one pushed commit."""
    root = tmp_path / "root"
    root.mkdir()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    alpha = root / "alpha"
    git(tmp_path, "clone", "-q", str(remote), str(alpha))
    commit(alpha, "a.txt", "1\n", "initial")
    git(alpha, "push", "-q", "-u", "origin", "main")
    return alpha


def test_clean_repo(repo):
    status = inspect_repo(repo)
    assert status.clean
    assert status.branch == "main"
    assert status.upstream == "origin/main"
    assert status.last_commit is not None
    assert status.last_commit.subject == "initial"


def test_categorises_changes(repo):
    (repo / "staged.txt").write_text("s\n")
    git(repo, "add", "staged.txt")
    (repo / "a.txt").write_text("changed\n")
    (repo / "new file.txt").write_text("u\n")  # a space checks -z parsing
    status = inspect_repo(repo)
    assert status.staged == ["A  staged.txt"]
    assert status.unstaged == ["M  a.txt"]
    assert status.untracked == ["new file.txt"]
    assert not status.clean


def test_rename_shows_both_paths(repo):
    git(repo, "mv", "a.txt", "b.txt")
    assert inspect_repo(repo).staged == ["R  a.txt -> b.txt"]


def test_ahead_and_stash(repo):
    commit(repo, "b.txt", "x\n", "local")
    (repo / "a.txt").write_text("stash me\n")
    git(repo, "stash", "-q")
    status = inspect_repo(repo)
    assert (status.ahead, status.behind) == (1, 0)
    assert status.stashes == 1
    assert not status.clean


def test_behind_is_seen_only_after_fetch(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(other))
    commit(other, "b.txt", "x\n", "remote change")
    git(other, "push", "-q", "origin", "main")
    assert inspect_repo(repo).behind == 0
    assert inspect_repo(repo, fetch=True).behind == 1


def test_conflict_and_operation(repo):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "a.txt", "side\n", "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, "a.txt", "main\n", "main")
    subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True)
    status = inspect_repo(repo)
    assert status.conflicted == ["a.txt"]
    assert status.operation == "merge"


def test_detached_head_and_no_upstream(repo):
    git(repo, "checkout", "-q", "--detach")
    status = inspect_repo(repo)
    assert status.branch is None
    assert status.upstream is None
    assert status.clean
    (repo / "new.txt").write_text("x\n")
    report = render_report([inspect_repo(repo)])
    assert "[detached HEAD]  no upstream" in report


def test_repo_without_commits(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    status = inspect_repo(empty)
    assert status.error is None
    assert status.last_commit is None


def test_git_failure_is_recorded_not_raised(tmp_path):
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    status = inspect_repo(broken)
    assert status.error
    assert "Needs attention" in render_report([status])


def test_report_caps_files(repo):
    for i in range(5):
        (repo / f"u{i}.txt").write_text("x\n")
    report = render_report([inspect_repo(repo)], max_files=2)
    assert "untracked (5):" in report
    assert "... 3 more" in report
    assert "u4.txt" not in render_report([inspect_repo(repo)], max_files=2)
    assert "u4.txt" in render_report([inspect_repo(repo)], max_files=0)


def test_exit_codes(repo):
    root = repo.parent
    out: list[str] = []
    assert status_all(root, out=out.append) == 0
    assert "Clean: alpha" in out[0]
    (repo / "new.txt").write_text("x\n")
    assert status_all(root, out=out.append) == 1
    (root / "broken" / ".git").mkdir(parents=True)
    assert status_all(root, out=out.append) == 2


def test_no_repos(tmp_path):
    out: list[str] = []
    assert status_all(tmp_path, out=out.append) == 0
    assert "No git repos found" in out[0]


def test_failed_fetch_is_never_a_false_all_clear(repo):
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    status = inspect_repo(repo, fetch=True)
    assert status.clean
    assert status.fetch_error
    assert "fetch failed" in render_report([status])
    assert "Clean:" not in render_report([status])
    out: list[str] = []
    assert status_all(repo.parent, fetch=True, out=out.append) == 2


def test_deleted_upstream_is_reported_as_gone(repo):
    git(repo, "push", "-q", "-u", "origin", "HEAD:feature")
    git(repo, "checkout", "-q", "-B", "feature", "--track", "origin/feature")
    git(repo, "push", "-q", "origin", "--delete", "feature")
    status = inspect_repo(repo)
    assert status.upstream == "origin/feature"
    assert status.upstream_gone


def test_parse_porcelain_rejects_unknown_records():
    with pytest.raises(ValueError, match="unexpected status record"):
        parse_porcelain(RepoStatus(path=Path("x")), "9 surprise\0")


def test_parse_porcelain_rejects_truncated_rename():
    record = "2 R. N... 100644 100644 100644 aaa bbb R100 new.txt\0"
    with pytest.raises(ValueError, match="truncated rename"):
        parse_porcelain(RepoStatus(path=Path("x")), record)


def test_non_negative_int():
    assert non_negative_int("0") == 0
    with pytest.raises(argparse.ArgumentTypeError):
        non_negative_int("-1")


def test_main_rejects_negative_max_files(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--max-files", "-3"])
    assert exc.value.code == 2
    assert "must be 0 or more" in capsys.readouterr().err


def test_commit_with_empty_subject_does_not_crash(repo):
    (repo / "e.txt").write_text("x\n")
    git(repo, "add", "e.txt")
    git(repo, "commit", "-q", "--allow-empty-message", "-m", "")
    status = inspect_repo(repo)
    assert status.error is None
    assert status.last_commit is not None
    assert status.last_commit.subject == ""


def test_clean_list_flags_repos_git_cannot_fully_vouch_for(repo):
    git(repo, "checkout", "-q", "-b", "local-only")
    report = render_report([inspect_repo(repo)])
    assert report == "Clean: alpha (no upstream)\n"
