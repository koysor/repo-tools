import argparse
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from check_all import (
    STEPS,
    RepoResult,
    Status,
    StepResult,
    check_all,
    check_repo,
    default_command,
    dependency_names,
    main,
    plan,
    python_repos,
    render_report,
    step_list,
)

RUFF_PYTEST = """\
[project]
name = "{name}"
dependencies = ["numpy>=2"]

[dependency-groups]
dev = ["ruff>=0.17", "pytest>=9", {{include-group = "other"}}]
"""


def make_repo(root: Path, name: str, pyproject: str | None = RUFF_PYTEST) -> Path:
    path = root / name
    (path / ".git").mkdir(parents=True)
    if pyproject is not None:
        (path / "pyproject.toml").write_text(pyproject.format(name=name))
    return path


class FakeUv:
    """Stands in for uv: returns a chosen exit code per (repo, command word)."""

    def __init__(self, codes: dict[tuple[str, str], int] | None = None, output=""):
        self.codes = codes or {}
        self.output = output
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(
        self, path: Path, args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append((path.name, args))
        # "sync", or the tool after `run --no-sync`, plus its subcommand.
        key = args[0] if args[0] == "sync" else " ".join(args[2:4])
        code = self.codes.get((path.name, key), 0)
        return subprocess.CompletedProcess(args, code, self.output, None)


def step(repo: RepoResult, name: str) -> StepResult:
    result = repo.get(name)
    assert result is not None, f"no {name} result"
    return result


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


def test_dependency_names_reads_every_table_and_normalises():
    pyproject = {
        "project": {
            "dependencies": ["NumPy>=2", "pytest_cov"],
            "optional-dependencies": {"docs": ["mkdocs-material[imaging]"]},
        },
        "dependency-groups": {"dev": ["ruff==0.17.4", {"include-group": "x"}]},
    }
    assert dependency_names(pyproject) == {
        "numpy",
        "pytest-cov",
        "mkdocs-material",
        "ruff",
    }


def test_default_commands(root):
    path = make_repo(root, "alpha")
    (path / "tests").mkdir()
    pyproject = {"dependency-groups": {"dev": ["ruff", "pytest", "ty"]}}
    assert default_command("lint", path, pyproject) == ["ruff", "check", "."]
    assert default_command("format", path, pyproject) == [
        "ruff",
        "format",
        "--check",
        ".",
    ]
    assert default_command("types", path, pyproject) == ["ty", "check"]
    assert default_command("tests", path, pyproject) == ["pytest"]


def test_mypy_targets_src_then_files_config(root):
    path = make_repo(root, "alpha")
    pyproject: dict = {"project": {"dependencies": ["mypy"]}}
    assert default_command("types", path, pyproject) == ["mypy", "."]
    (path / "src").mkdir()
    assert default_command("types", path, pyproject) == ["mypy", "src"]
    pyproject["tool"] = {"mypy": {"files": ["src/pkg"]}}
    assert default_command("types", path, pyproject) == ["mypy"]


def test_ty_wins_over_mypy(root):
    path = make_repo(root, "alpha")
    pyproject = {"project": {"dependencies": ["mypy", "ty"]}}
    assert default_command("types", path, pyproject) == ["ty", "check"]


def test_missing_tools_give_reasons(root):
    path = make_repo(root, "alpha")
    assert "ruff" in default_command("lint", path, {})
    assert "type checker" in default_command("types", path, {})
    assert "pytest" in default_command("tests", path, {})
    no_tests = default_command("tests", path, {"project": {"dependencies": ["pytest"]}})
    assert "no tests" in no_tests


def test_testpaths_count_as_tests(root):
    path = make_repo(root, "alpha")
    pyproject = {
        "project": {"dependencies": ["pytest"]},
        "tool": {"pytest": {"ini_options": {"testpaths": ["checks"]}}},
    }
    assert default_command("tests", path, pyproject) == ["pytest"]


def test_overrides(root):
    path = make_repo(root, "alpha")
    pyproject = {
        "project": {"dependencies": ["ruff"]},
        "tool": {
            "repo-tools": {
                "check-all": {"lint": ["ruff", "check", "src"], "format": False}
            }
        },
    }
    assert plan(path, pyproject, "lint") == ["ruff", "check", "src"]
    assert "switched off" in plan(path, pyproject, "format")


@pytest.mark.parametrize("bad", [True, "ruff check", [], ["ruff", 1], [""]])
def test_bad_override_is_rejected(root, bad):
    path = make_repo(root, "alpha")
    pyproject = {"tool": {"repo-tools": {"check-all": {"lint": bad}}}}
    with pytest.raises(ValueError, match="must be false or a list"):
        plan(path, pyproject, "lint")


def test_bad_override_is_an_error_not_a_crash(root):
    path = make_repo(
        root,
        "alpha",
        RUFF_PYTEST + '\n[tool.repo-tools.check-all]\nlint = "ruff check"\n',
    )
    result = check_repo(path, run=FakeUv())
    assert step(result, "lint").status is Status.ERROR
    assert result.errored


def test_check_repo_runs_sync_then_steps(root):
    path = make_repo(root, "alpha")
    (path / "tests").mkdir()
    uv = FakeUv()
    result = check_repo(path, run=uv)
    assert [args for _, args in uv.calls] == [
        ["sync", "--locked"],
        ["run", "--no-sync", "ruff", "check", "."],
        ["run", "--no-sync", "ruff", "format", "--check", "."],
        ["run", "--no-sync", "pytest"],
    ]
    statuses = {r.step: r.status for r in result.results}
    assert statuses == {
        "sync": Status.PASS,
        "lint": Status.PASS,
        "format": Status.PASS,
        "types": Status.SKIP,
        "tests": Status.PASS,
    }
    assert not result.failed and not result.errored


def test_failed_sync_stops_the_repo(root):
    path = make_repo(root, "alpha")
    uv = FakeUv({("alpha", "sync"): 1})
    result = check_repo(path, run=uv)
    assert len(uv.calls) == 1
    assert step(result, "sync").status is Status.FAIL
    assert all(step(result, s).status is Status.NOT_RUN for s in STEPS)


def test_failing_step_does_not_stop_later_steps(root):
    path = make_repo(root, "alpha")
    (path / "tests").mkdir()
    uv = FakeUv({("alpha", "ruff check"): 1})
    result = check_repo(path, run=uv)
    assert step(result, "lint").status is Status.FAIL
    assert step(result, "tests").status is Status.PASS
    assert result.failed


def test_pytest_collecting_nothing_is_a_skip(root):
    path = make_repo(root, "alpha")
    (path / "tests").mkdir()
    result = check_repo(path, ["tests"], run=FakeUv({("alpha", "pytest"): 5}))
    assert step(result, "tests").status is Status.SKIP
    assert not result.failed


def test_timeout_is_an_error_with_partial_output(root):
    path = make_repo(root, "alpha")
    (path / "tests").mkdir()

    def slow(path, args, timeout):
        if args[0] == "sync":
            return subprocess.CompletedProcess(args, 0, "", None)
        raise subprocess.TimeoutExpired(args, timeout, output=b"half way")

    result = check_repo(path, ["tests"], run=slow, timeout=5)
    tests = step(result, "tests")
    assert tests.status is Status.ERROR
    assert tests.detail == "timed out after 5s"
    assert tests.output == "half way"


def test_missing_executable_is_an_error(root):
    path = make_repo(root, "alpha")

    def missing(path, args, timeout):
        raise FileNotFoundError("uv")

    result = check_repo(path, run=missing)
    assert step(result, "sync").status is Status.ERROR
    assert step(result, "lint").status is Status.NOT_RUN


def test_unreadable_pyproject_is_an_error(root):
    path = make_repo(root, "alpha", "not [valid toml")
    uv = FakeUv()
    result = check_repo(path, run=uv)
    assert not uv.calls
    assert step(result, "sync").status is Status.ERROR
    assert "pyproject.toml" in step(result, "sync").detail


def test_python_repos_splits_and_filters(root):
    make_repo(root, "alpha")
    make_repo(root, "notes", pyproject=None)
    (root / "not-a-repo").mkdir()
    repos, without = python_repos(root)
    assert [p.name for p in repos] == ["alpha"]
    assert without == ["notes"]
    assert [p.name for p in python_repos(root, ["alpha", "alpha"])[0]] == ["alpha"]
    with pytest.raises(LookupError, match="ghost"):
        python_repos(root, ["ghost"])


def test_report_table_failures_and_skips(root):
    alpha = make_repo(root, "alpha")
    (alpha / "tests").mkdir()
    beta = make_repo(root, "beta")
    results = [
        check_repo(alpha, run=FakeUv({("alpha", "ruff format"): 1}, "line1\nline2\n")),
        check_repo(beta, run=FakeUv()),
    ]
    report = render_report(results, STEPS, ["notes"], tail=1)
    header, alpha_row, beta_row = report.splitlines()[:3]
    assert header.split() == [
        "repo",
        "sync",
        "lint",
        "format",
        "types",
        "tests",
        "time",
    ]
    assert alpha_row.split()[:6] == ["alpha", "pass", "pass", "FAIL", "-", "pass"]
    assert beta_row.split()[:6] == ["beta", "pass", "pass", "pass", "-", "-"]
    assert "alpha format: FAIL (exit status 1)" in report
    assert "$ uv run --no-sync ruff format --check ." in report
    assert "  line2" in report and "line1" not in report  # tail=1
    assert "beta tests: no tests/ directory or testpaths" in report
    assert "No pyproject.toml: notes" in report
    assert "\033[" not in report


def test_exit_codes(root):
    make_repo(root, "alpha")
    out: list[str] = []
    assert check_all(root, run=FakeUv(), out=out.append) == 0
    assert (
        check_all(root, run=FakeUv({("alpha", "ruff check"): 1}), out=out.append) == 1
    )

    def broken(path, args, timeout):
        raise OSError("boom")

    assert check_all(root, run=broken, out=out.append) == 2


def test_no_python_repos(root):
    make_repo(root, "notes", pyproject=None)
    out: list[str] = []
    assert check_all(root, run=FakeUv(), out=out.append) == 0
    assert "No Python repos" in out[0]


def test_steps_subset_runs_only_those(root):
    make_repo(root, "alpha")
    uv = FakeUv()
    out: list[str] = []
    check_all(root, steps=["lint"], run=uv, out=out.append)
    assert [args for _, args in uv.calls] == [
        ["sync", "--locked"],
        ["run", "--no-sync", "ruff", "check", "."],
    ]
    assert out[0].splitlines()[0].split() == ["repo", "sync", "lint", "time"]


def test_progress_is_reported_per_repo(root):
    make_repo(root, "alpha")
    seen: list[str] = []
    check_all(root, run=FakeUv(), out=lambda _: None, progress=seen.append)
    assert seen and seen[0].startswith("alpha: ok")


def test_step_list():
    assert step_list("tests,lint") == ["lint", "tests"]
    for bad in ("", "lint,deploy"):
        with pytest.raises(argparse.ArgumentTypeError):
            step_list(bad)


def test_main_reports_unknown_repo(root, capsys):
    make_repo(root, "alpha")
    assert main(["--root", str(root), "--repo", "ghost"]) == 2
    assert "no git repo named ghost" in capsys.readouterr().err


def test_main_rejects_bad_jobs(capsys):
    with pytest.raises(SystemExit):
        main(["--jobs", "0"])
    assert "must be 1 or more" in capsys.readouterr().err


def test_child_env_drops_virtual_env(monkeypatch):
    from check_all import child_env

    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere/.venv")
    monkeypatch.setenv("FORCE_COLOR", "1")
    env = child_env()
    assert "VIRTUAL_ENV" not in env and "FORCE_COLOR" not in env
    assert env["NO_COLOR"] == "1"


def test_real_uv_runs_in_the_repo(root):
    """One end-to-end call through real uv, if it is installed."""
    from check_all import run_uv

    if not __import__("shutil").which("uv"):
        pytest.skip("uv not installed")
    done = run_uv(root, ["--version"], 30)
    assert done.returncode == 0 and done.stdout.startswith("uv ")
