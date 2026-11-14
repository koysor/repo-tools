import argparse
import json
import subprocess
from pathlib import Path

import pytest

from repo_status import (
    CiStatus,
    RepoStatus,
    WorkflowRun,
    inspect_repo,
    latest_per_workflow,
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


# --- CI status (--ci) -------------------------------------------------------


def run_data(workflow="Tests", status="completed", conclusion="success", sha="a" * 40):
    return {
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "headSha": sha,
        "url": f"https://github.com/koysor/alpha/actions/runs/{workflow}",
        "createdAt": "2026-11-14T09:00:00Z",
        "event": "push",
    }


class FakeGh:
    """Stands in for gh: canned `run list` JSON, recording each call."""

    def __init__(self, runs=None, rc=0, stdout=None):
        self.runs = runs or []
        self.rc = rc
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        assert args[:2] == ["run", "list"], args
        if self.rc:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")
        body = self.stdout if self.stdout is not None else json.dumps(self.runs)
        return subprocess.CompletedProcess(args, 0, body, "")


@pytest.fixture
def github_repo(repo):
    """`repo` with a GitHub origin URL, so the gh slug lookup succeeds.

    Nothing is fetched or pushed afterwards, so the URL is never contacted.
    """
    git(repo, "remote", "set-url", "origin", "https://github.com/koysor/alpha.git")
    return repo


def test_latest_per_workflow_keeps_newest_of_each():
    runs = latest_per_workflow(
        [
            run_data("Tests", conclusion="failure"),
            run_data("Lint"),
            run_data("Tests", conclusion="success"),
        ]
    )
    assert [(r.workflow, r.outcome) for r in runs] == [
        ("Lint", "passing"),
        ("Tests", "failing"),
    ]


@pytest.mark.parametrize(
    ("status", "conclusion", "outcome"),
    [
        ("completed", "success", "passing"),
        ("completed", "skipped", "passing"),
        ("completed", "neutral", "passing"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "failure", "failing"),
        ("completed", "timed_out", "failing"),
        ("completed", "startup_failure", "failing"),
        ("in_progress", "", "running"),
        ("queued", "", "running"),
    ],
)
def test_run_outcomes(status, conclusion, outcome):
    run = WorkflowRun.from_gh(run_data(status=status, conclusion=conclusion))
    assert run.outcome == outcome


def test_overall_outcome_is_the_worst():
    def ci(*conclusions):
        return CiStatus(
            [
                WorkflowRun.from_gh(run_data(f"w{i}", conclusion=c))
                for i, c in enumerate(conclusions)
            ]
        )

    assert ci().outcome == "no runs"
    assert ci("success", "success").outcome == "passing"
    assert ci("success", "cancelled").outcome == "cancelled"
    assert ci("success", "failure", "cancelled").outcome == "failing"


def test_ci_is_listed_for_the_upstream_branch(github_repo):
    gh = FakeGh([run_data()])
    status = inspect_repo(github_repo, gh=gh)
    assert gh.calls[0][:6] == ["run", "list", "-R", "koysor/alpha", "--branch", "main"]
    assert status.ci is not None and status.ci.outcome == "passing"
    assert status.upstream_sha == git(github_repo, "rev-parse", "HEAD")


def test_ci_not_asked_for_means_no_gh_calls(github_repo):
    status = inspect_repo(github_repo)
    assert status.ci is None and status.ci_note is None


def test_failing_ci_sets_exit_code_and_shows_url(github_repo):
    head = git(github_repo, "rev-parse", "HEAD")
    gh = FakeGh(
        [run_data("Tests", conclusion="failure", sha=head), run_data("Lint", sha=head)]
    )
    out: list[str] = []
    assert status_all(github_repo.parent, gh=gh, out=out.append) == 1
    report = out[0]
    assert "alpha  failing  failing Tests" in report
    assert (
        "Tests: failure  https://github.com/koysor/alpha/actions/runs/Tests" in report
    )
    assert "nothing has run" not in report


def test_passing_ci_keeps_a_clean_exit(github_repo):
    head = git(github_repo, "rev-parse", "HEAD")
    out: list[str] = []
    assert (
        status_all(github_repo.parent, gh=FakeGh([run_data(sha=head)]), out=out.append)
        == 0
    )
    assert "alpha  passing  1 workflows" in out[0]


def test_ci_without_a_run_on_the_pushed_commit_is_noted(github_repo):
    out: list[str] = []
    status_all(github_repo.parent, gh=FakeGh([run_data(sha="b" * 40)]), out=out.append)
    head = git(github_repo, "rev-parse", "--short=7", "HEAD")
    assert f"(nothing has run on {head} yet)" in out[0]


def test_repo_with_no_workflow_runs(github_repo):
    out: list[str] = []
    assert status_all(github_repo.parent, gh=FakeGh([]), out=out.append) == 0
    assert "alpha  no runs" in out[0]


def test_gh_failure_needs_attention(github_repo):
    out: list[str] = []
    assert status_all(github_repo.parent, gh=FakeGh(rc=1), out=out.append) == 2
    assert "alpha: CI status unknown: gh run list failed: HTTP 404" in out[0]


def test_gh_invalid_json_needs_attention(github_repo):
    status = inspect_repo(github_repo, gh=FakeGh(stdout="not json"))
    assert status.ci is None
    assert status.ci_error is not None and "invalid JSON" in status.ci_error


def test_non_github_origin_is_noted_not_an_error(repo):
    out: list[str] = []
    assert status_all(repo.parent, gh=FakeGh(), out=out.append) == 0
    assert "alpha  not checked: origin is not on GitHub" in out[0]


def test_detached_head_skips_ci(github_repo):
    git(github_repo, "checkout", "-q", "--detach")
    gh = FakeGh()
    status = inspect_repo(github_repo, gh=gh)
    assert not gh.calls
    assert status.ci_note == "detached HEAD"


def test_main_needs_gh_for_ci(monkeypatch, capsys):
    import repo_status

    real_which = repo_status.shutil.which
    monkeypatch.setattr(
        repo_status.shutil,
        "which",
        lambda tool: None if tool == "gh" else real_which(tool),
    )
    assert main(["--ci"]) == 2
    assert "gh was not found" in capsys.readouterr().err
