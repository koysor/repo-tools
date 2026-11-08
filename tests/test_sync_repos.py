import argparse
import importlib
import subprocess
from pathlib import Path

import pytest

import sync_repos
from sync_repos import (
    FIELD_SEP,
    Choice,
    Commit,
    Outcome,
    Result,
    decide,
    discover_repos,
    in_progress_operation,
    inspect_repo,
    pull_rebase,
    render_report,
    render_summary,
    sync,
)


def git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=check
    )
    return result.stdout.strip()


def commit(repo: Path, filename: str, content: str, message: str) -> None:
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", message)


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


def add_beta(root: Path, tmp_path: Path) -> Path:
    beta = root / "beta"
    git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(beta))
    return beta


def collect():
    lines: list[str] = []
    return lines, lines.append


def fail_git(monkeypatch, command: str, returncode=128, stderr="fatal: boom"):
    """Make one git subcommand fail while everything else runs for real."""
    real = sync_repos.run_git

    def fake(path, *args, **kwargs):
        if args[0] == command:
            return subprocess.CompletedProcess(args, returncode, "", stderr)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sync_repos, "run_git", fake)


def conflict_midway(alpha: Path, other: Path):
    """Leave alpha in a half-resolved rebase and return its pre-rebase state."""
    commit(alpha, "a.txt", "local\n", "local edit")
    commit(other, "a.txt", "remote\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    state = inspect_repo(alpha)
    git(alpha, "rebase", "origin/main", check=False)
    assert in_progress_operation(alpha) == "rebase"
    return state


# --- discovery and inspection -------------------------------------------------


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


def test_no_upstream_and_detached_are_reported(world):
    _, alpha, _ = world
    git(alpha, "checkout", "-q", "-b", "scratch")
    assert "no upstream" in (inspect_repo(alpha).problem or "")
    git(alpha, "checkout", "-q", "--detach")
    assert inspect_repo(alpha).problem == "detached HEAD"


def test_fetch_failure_is_reported_not_raised(world):
    _, alpha, _ = world
    git(alpha, "remote", "set-url", "origin", str(alpha.parent / "missing.git"))
    state = inspect_repo(alpha)
    assert "fetch failed" in (state.problem or "")


def test_fetch_timeout_is_a_fetch_error(world, monkeypatch):
    _, alpha, _ = world
    real = sync_repos.run_git

    def fake(path, *args, **kwargs):
        if args[0] == "fetch":
            raise subprocess.TimeoutExpired(args, 1)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sync_repos, "run_git", fake)
    assert "timed out" in (inspect_repo(alpha).problem or "")


def test_dependency_file_in_non_ascii_directory_is_detected(world):
    _, alpha, other = world
    push_from_other(other, filename="é/uv.lock", message="bump")
    assert inspect_repo(alpha).touches_dependencies


# --- failing git commands must not look benign --------------------------------


def test_status_failure_is_a_problem_not_a_clean_repo(world, monkeypatch):
    _, alpha, _ = world
    fail_git(monkeypatch, "status")
    state = inspect_repo(alpha)
    assert "git status failed" in (state.problem or "")


def test_rev_list_failure_is_not_reported_up_to_date(world, monkeypatch):
    root, alpha, _ = world
    fail_git(monkeypatch, "rev-list")
    lines, out = collect()
    assert inspect_repo(alpha).problem
    assert sync(root, check=True, out=out) == 2
    assert "Up to date" not in "".join(lines)


def test_fatal_symbolic_ref_is_not_called_detached_head(world, monkeypatch):
    _, alpha, _ = world
    fail_git(monkeypatch, "symbolic-ref")
    assert inspect_repo(alpha).problem != "detached HEAD"


def test_exception_in_one_repo_does_not_hide_the_others(world, tmp_path, monkeypatch):
    root, _, other = world
    beta = add_beta(root, tmp_path)
    push_from_other(other)
    real = sync_repos.run_git

    def fake(path, *args, **kwargs):
        if path.name == "alpha":
            raise OSError("boom")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sync_repos, "run_git", fake)
    lines, out = collect()
    assert sync(root, check=True, out=out) == 2
    text = "".join(lines)
    assert "alpha: boom" in text
    assert f"{beta.name}  [main]  1 commit behind" in text


# --- rebasing -----------------------------------------------------------------


def test_pull_rebase_updates_and_summarises(world):
    _, alpha, other = world
    push_from_other(other)
    outcome = pull_rebase(inspect_repo(alpha))
    assert outcome.result is Result.UPDATED and outcome.commits == 1
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
    assert pull_rebase(state).result is Result.UPDATED
    assert git(alpha, "log", "--format=%s", "-2").splitlines()[0] == "local work"


def test_local_merge_commit_survives_the_rebase(world):
    _, alpha, other = world
    git(alpha, "checkout", "-q", "-b", "feat")
    commit(alpha, "f.txt", "f\n", "feature")
    git(alpha, "checkout", "-q", "main")
    commit(alpha, "m.txt", "m\n", "on main")
    git(alpha, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    push_from_other(other)
    assert pull_rebase(inspect_repo(alpha)).result is Result.UPDATED
    assert git(alpha, "rev-list", "--merges", "--count", "origin/main..HEAD") == "1"


def test_rebases_onto_the_reviewed_commit_not_newer_ones(world):
    _, alpha, other = world
    push_from_other(other, filename="b.txt")
    state = inspect_repo(alpha)
    push_from_other(other, filename="c.txt")  # lands after the report
    assert pull_rebase(state).result is Result.UPDATED
    assert (alpha / "b.txt").exists() and not (alpha / "c.txt").exists()


def test_head_moving_after_the_report_is_skipped(world):
    _, alpha, other = world
    push_from_other(other)
    state = inspect_repo(alpha)
    commit(alpha, "late.txt", "x\n", "late local commit")
    outcome = pull_rebase(state)
    assert outcome.result is Result.SKIPPED and "HEAD moved" in outcome.detail


def test_conflict_is_aborted_and_repo_left_clean(world):
    _, alpha, other = world
    commit(alpha, "a.txt", "local\n", "local edit")
    commit(other, "a.txt", "remote\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    before = git(alpha, "rev-parse", "HEAD")
    outcome = pull_rebase(inspect_repo(alpha))
    assert outcome.result is Result.CONFLICT
    assert git(alpha, "rev-parse", "HEAD") == before
    assert git(alpha, "status", "--porcelain") == ""
    assert in_progress_operation(alpha) is None


def test_rebase_the_user_started_is_reported_and_never_aborted(world):
    root, alpha, other = world
    state = conflict_midway(alpha, other)

    outcome = pull_rebase(state)  # state predates the user's rebase
    assert outcome.result is Result.SKIPPED
    assert in_progress_operation(alpha) == "rebase"

    assert inspect_repo(alpha).problem == "rebase in progress"
    assert sync(root, assume_yes=True, out=lambda _: None) == 0
    assert in_progress_operation(alpha) == "rebase"


# --- exit codes and prompting -------------------------------------------------


def test_check_mode_exit_codes_and_no_changes(world):
    root, alpha, other = world
    _, out = collect()
    assert sync(root, check=True, out=out) == 0
    push_from_other(other)
    assert sync(root, check=True, out=out) == 1
    assert not (alpha / "b.txt").exists()


def test_check_mode_exits_2_when_a_repo_cannot_be_checked(world):
    root, alpha, _ = world
    git(alpha, "remote", "set-url", "origin", str(root / "missing.git"))
    assert sync(root, check=True, out=lambda _: None) == 2


@pytest.mark.parametrize(
    "answer, pulled", [("y", True), ("n", False), ("", False), ("q", False)]
)
def test_prompt_answers(world, answer, pulled):
    root, alpha, other = world
    push_from_other(other)
    _, out = collect()
    sync(root, prompt=lambda _: answer, out=out)
    assert (alpha / "b.txt").exists() is pulled


def test_answer_all_pulls_every_repo(world, tmp_path):
    root, alpha, other = world
    beta = add_beta(root, tmp_path)
    push_from_other(other)
    answers = iter(["a"])
    sync(root, prompt=lambda _: next(answers), out=lambda _: None)
    assert (alpha / "b.txt").exists() and (beta / "b.txt").exists()


def test_quit_skips_the_remaining_repos(world, tmp_path):
    root, alpha, other = world
    beta = add_beta(root, tmp_path)
    push_from_other(other)
    answers = iter(["y", "q"])
    sync(root, prompt=lambda _: next(answers), out=lambda _: None)
    assert (alpha / "b.txt").exists() and not (beta / "b.txt").exists()


def test_end_of_input_quits_cleanly(world):
    root, alpha, other = world
    push_from_other(other)

    def eof(_):
        raise EOFError

    assert sync(root, prompt=eof, out=lambda _: None) == 0
    assert not (alpha / "b.txt").exists()


def test_ctrl_c_prints_the_partial_summary_and_exits_130(world, tmp_path):
    root, alpha, other = world
    beta = add_beta(root, tmp_path)
    push_from_other(other)
    answers = iter(["y"])

    def interrupt_second(_):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    lines, out = collect()
    assert sync(root, prompt=interrupt_second, out=out) == 130
    assert (alpha / "b.txt").exists() and not (beta / "b.txt").exists()
    assert "alpha: updated" in "".join(lines)


def test_yes_flag_pulls_without_prompting(world):
    root, alpha, other = world
    push_from_other(other)

    def fail(_):
        raise AssertionError("should not prompt")

    assert sync(root, assume_yes=True, prompt=fail, out=lambda _: None) == 0
    assert (alpha / "b.txt").exists()


def test_dirty_repo_is_skipped_but_others_still_pulled(world, tmp_path):
    root, alpha, other = world
    beta = add_beta(root, tmp_path)
    push_from_other(other)
    (alpha / "a.txt").write_text("dirty\n")
    lines, out = collect()
    sync(root, prompt=lambda _: "a", out=out)
    assert not (alpha / "b.txt").exists() and (beta / "b.txt").exists()
    assert "alpha: skipped" in "".join(lines)


def test_conflict_makes_run_exit_nonzero(world):
    root, alpha, other = world
    commit(alpha, "a.txt", "local\n", "local edit")
    commit(other, "a.txt", "remote\n", "remote edit")
    git(other, "push", "-q", "origin", "main")
    assert sync(root, assume_yes=True, out=lambda _: None) == 1


# --- decide, summaries, main --------------------------------------------------


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("y", (Choice.YES, False)),
        ("YES", (Choice.YES, False)),
        ("a", (Choice.YES, True)),
        ("q", (Choice.QUIT, False)),
        ("", (Choice.NO, False)),
        ("what", (Choice.NO, False)),
    ],
)
def test_decide_answers(world, answer, expected):
    _, alpha, other = world
    push_from_other(other)
    state = inspect_repo(alpha)
    assert decide(state, lambda _: answer, False, False) == expected


def test_decide_warns_about_unpushed_commits(world):
    _, alpha, other = world
    commit(alpha, "local.txt", "l\n", "local work")
    push_from_other(other)
    asked: list[str] = []
    decide(inspect_repo(alpha), lambda q: asked.append(q) or "n", False, False)
    assert "1 unpushed local commit(s) will be rebased" in asked[0]


def test_decide_never_prompts_with_yes_or_after_all(world):
    _, alpha, _ = world
    state = inspect_repo(alpha)

    def fail(_):
        raise AssertionError("should not prompt")

    assert decide(state, fail, True, False) == (Choice.YES, False)
    assert decide(state, fail, False, True) == (Choice.YES, True)


def test_render_summary_covers_every_outcome():
    text = render_summary(
        [
            Outcome("a", Result.DECLINED),
            Outcome("b", Result.SKIPPED, "uncommitted changes"),
            Outcome("c", Result.CONFLICT, "aborted"),
            Outcome("d", Result.FAILED, "no network"),
        ]
    )
    assert "a: declined" in text
    assert "b: skipped (uncommitted changes)" in text
    assert "c: conflict (aborted)" in text
    assert "d: failed (no network)" in text
    assert "0 updated, 2 with problems." in text
    assert render_summary([]) == "Nothing was pulled.\n"


def test_main_without_a_terminal_behaves_like_check(world, capsys, monkeypatch):
    root, alpha, other = world
    push_from_other(other)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert sync_repos.main(["--root", str(root)]) == 1
    assert "1 commit behind" in capsys.readouterr().out
    assert not (alpha / "b.txt").exists()


def test_main_yes_pulls(world, monkeypatch):
    root, alpha, other = world
    push_from_other(other)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert sync_repos.main(["--root", str(root), "--yes"]) == 0
    assert (alpha / "b.txt").exists()


def test_main_reports_missing_git(world, capsys, monkeypatch):
    root, _, _ = world
    monkeypatch.setattr(sync_repos.shutil, "which", lambda _: None)
    assert sync_repos.main(["--root", str(root)]) == 2
    assert "git was not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], sync_repos.DEFAULT_ROOT),
        (["--root"], sync_repos.DEFAULT_ROOT),
        (["--root", "--check"], sync_repos.DEFAULT_ROOT),
        (["--root", "elsewhere"], Path("elsewhere")),
    ],
)
def test_root_argument_value_is_optional(argv, expected):
    parser = argparse.ArgumentParser()
    sync_repos.add_root_argument(parser)
    parser.add_argument("--check", action="store_true")
    assert parser.parse_args(argv).root == expected


def test_default_root_is_parent_of_repo_tools():
    assert Path(sync_repos.__file__).resolve().parent.parent == sync_repos.DEFAULT_ROOT


@pytest.mark.parametrize("module", ["sync_repos", "repo_status", "review_prs"])
def test_every_script_accepts_bare_root(module, monkeypatch):
    """A bare --root parses in each script and scans the default root."""
    script = importlib.import_module(module)
    seen = []
    monkeypatch.setattr(script, "discover_repos", lambda root: seen.append(root) or [])
    monkeypatch.setattr(script.shutil, "which", lambda _: "found")
    if module == "review_prs":
        monkeypatch.setattr(
            script,
            "run_gh",
            lambda args: subprocess.CompletedProcess(list(args), 0, "", ""),
        )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert script.main(["--root"]) == 0
    assert seen == [sync_repos.DEFAULT_ROOT]


def test_commit_parse_tolerates_stripped_empty_subject():
    commit = Commit.parse(f"abc1234{FIELD_SEP}2026-01-01{FIELD_SEP}Tester")
    assert commit == Commit("abc1234", "2026-01-01", "Tester", "")
