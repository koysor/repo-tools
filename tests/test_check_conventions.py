import json
import subprocess
from pathlib import Path

import pytest

from check_conventions import (
    Status,
    check_conventions,
    check_python,
    check_repo,
    check_standalone,
    imported_modules,
    main,
    render_report,
)

PYPROJECT = """\
[project]
name = "{name}"
requires-python = "{python}"
dependencies = [{deps}]
"""


def git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(
    root: Path,
    name: str,
    *,
    python: str | None = ">=3.14",
    deps: str = "",
    lock: bool = True,
    commit_lock: bool = True,
    ci: bool = True,
    dependabot: bool = True,
    pre_commit: bool = True,
    readme: bool = True,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    if readme:
        (path / "README.md").write_text("# x\n")
    if python is not None:
        (path / "pyproject.toml").write_text(
            PYPROJECT.format(name=name, python=python, deps=deps)
        )
        if lock:
            (path / "uv.lock").write_text("version = 1\n")
            if commit_lock:
                git(path, "add", "uv.lock")
        if ci:
            (path / ".github" / "workflows").mkdir(parents=True)
            (path / ".github" / "workflows" / "tests.yml").write_text("on: push\n")
        if dependabot:
            (path / ".github").mkdir(exist_ok=True)
            (path / ".github" / "dependabot.yml").write_text("version: 2\n")
        if pre_commit:
            (path / ".pre-commit-config.yaml").write_text("repos: []\n")
    return path


def statuses(report) -> dict[str, Status]:
    return {r.check: r.status for r in report.results}


def test_compliant_python_repo(tmp_path):
    path = make_repo(tmp_path, "app")
    report = check_repo(path, [path])
    assert statuses(report) == {
        "readme": Status.OK,
        "python": Status.OK,
        "lock": Status.OK,
        "ci": Status.OK,
        "dependabot": Status.OK,
        "pre-commit": Status.OK,
        "standalone": Status.NA,
    }
    assert not report.failed


def test_non_python_repo_only_needs_a_readme(tmp_path):
    path = make_repo(tmp_path, "notes", python=None)
    report = check_repo(path, [path])
    assert statuses(report)["readme"] is Status.OK
    assert {s for c, s in statuses(report).items() if c != "readme"} == {Status.NA}
    assert not report.failed


def test_missing_pieces_fail(tmp_path):
    path = make_repo(
        tmp_path, "app", readme=False, lock=False, ci=False, dependabot=False
    )
    report = check_repo(path, [path])
    got = statuses(report)
    assert got["readme"] is got["lock"] is got["ci"] is got["dependabot"] is Status.FAIL
    assert report.failed


def test_uncommitted_lock_fails(tmp_path):
    path = make_repo(tmp_path, "app", commit_lock=False)
    result = check_repo(path, [path]).get("lock")
    assert result is not None
    assert (result.status, result.detail) == (Status.FAIL, "uv.lock is not committed")


def test_missing_pre_commit_only_warns(tmp_path):
    path = make_repo(tmp_path, "app", pre_commit=False)
    report = check_repo(path, [path])
    assert statuses(report)["pre-commit"] is Status.WARN
    assert not report.failed


@pytest.mark.parametrize(
    ("spec", "version_file", "status"),
    [
        (">=3.14", None, Status.OK),
        (">= 3.14", None, Status.OK),
        ("==3.14.8", None, Status.OK),
        ("==3.14.8", "3.14.8", Status.OK),
        ("==3.14.8", "3.14.7", Status.FAIL),
        (">=3.13", None, Status.FAIL),
        (">=3.14,<3.15", None, Status.FAIL),
        ("==3.13.2", None, Status.FAIL),
    ],
)
def test_requires_python(tmp_path, spec, version_file, status):
    if version_file:
        (tmp_path / ".python-version").write_text(version_file + "\n")
    pyproject = {"project": {"requires-python": spec}}
    assert check_python(tmp_path, pyproject).status is status


def test_missing_requires_python_fails(tmp_path):
    assert check_python(tmp_path, {"project": {}}).status is Status.FAIL


def test_unreadable_pyproject_fails(tmp_path):
    path = make_repo(tmp_path, "app")
    (path / "pyproject.toml").write_text("not [valid toml")
    result = check_repo(path, [path]).get("python")
    assert result is not None
    assert result.status is Status.FAIL
    assert "cannot read pyproject.toml" in result.detail


@pytest.mark.parametrize(
    ("line", "modules"),
    [
        ("import quant_finance", ["quant_finance"]),
        ("from quant_finance.bonds import price", ["quant_finance"]),
        ("import numpy as np, quant_finance", ["numpy", "quant_finance"]),
        ("    from maths_lib import x", ["maths_lib"]),
        ("# import quant_finance", []),
        ("x = 'import quant_finance'", []),
    ],
)
def test_imported_modules(line, modules):
    assert imported_modules(line) == modules


@pytest.fixture
def workspace(tmp_path):
    lib = make_repo(tmp_path, "quant-finance")
    (lib / "src" / "quant_finance").mkdir(parents=True)
    (lib / "src" / "quant_finance" / "__init__.py").write_text("")
    (lib / "src" / "options").mkdir()  # no __init__.py: not a package
    cqf = make_repo(tmp_path, "cqf", python="==3.14.8")
    return cqf, lib


def test_standalone_ok_without_sibling_imports(workspace):
    cqf, lib = workspace
    (cqf / "notes.py").write_text("import numpy as np\nimport options\n")
    assert check_standalone(cqf, {}, [cqf, lib]).status is Status.OK


def test_standalone_catches_python_import(workspace):
    cqf, lib = workspace
    (cqf / "lessons").mkdir()
    (cqf / "lessons" / "bonds.py").write_text(
        "x = 1\nfrom quant_finance import bonds\n"
    )
    result = check_standalone(cqf, {}, [cqf, lib])
    assert result.status is Status.FAIL
    assert result.detail == "lessons/bonds.py:2 imports quant_finance (quant-finance)"


def test_standalone_catches_notebook_import(workspace):
    cqf, lib = workspace
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["import quant_finance"]},
            {
                "cell_type": "code",
                "source": ["%matplotlib inline\n", "import quant_finance\n"],
            },
        ]
    }
    (cqf / "lesson.ipynb").write_text(json.dumps(notebook))
    result = check_standalone(cqf, {}, [cqf, lib])
    assert result.status is Status.FAIL
    assert result.detail == "lesson.ipynb:2 imports quant_finance (quant-finance)"


def test_standalone_catches_dependency(workspace):
    cqf, lib = workspace
    pyproject = {"project": {"dependencies": ["Quant_Finance>=1"]}}
    result = check_standalone(cqf, pyproject, [cqf, lib])
    assert result.status is Status.FAIL
    assert "depends on quant-finance" in result.detail


def test_standalone_skips_hidden_folders(workspace):
    cqf, lib = workspace
    venv = cqf / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "x.py").write_text("import quant_finance\n")
    assert check_standalone(cqf, {}, [cqf, lib]).status is Status.OK


def test_only_standalone_repos_get_the_check(workspace):
    cqf, lib = workspace
    (lib / "uses_itself.py").write_text("import quant_finance\n")
    assert statuses(check_repo(lib, [cqf, lib]))["standalone"] is Status.NA
    assert statuses(check_repo(cqf, [cqf, lib]))["standalone"] is Status.OK


def test_render_report_lists_problems():
    from check_conventions import CheckResult, RepoReport

    reports = [
        RepoReport("app", [CheckResult("readme", Status.FAIL, "no README.md")]),
        RepoReport("lib", [CheckResult("pre-commit", Status.WARN, "no config")]),
    ]
    text = render_report(reports)
    lines = text.splitlines()
    assert lines[0].split() == [
        "repo",
        "readme",
        "python",
        "lock",
        "ci",
        "dependabot",
        "pre-commit",
        "standalone",
    ]
    assert lines[1].split()[:2] == ["app", "FAIL"]
    assert "FAIL app readme: no README.md" in lines
    assert "warn lib pre-commit: no config" in lines
    assert "\033[" not in text


def test_exit_codes(tmp_path):
    make_repo(tmp_path, "good")
    make_repo(tmp_path, "notes", python=None)
    assert check_conventions(tmp_path)[1] == 0
    make_repo(tmp_path, "bad", ci=False)
    report, code = check_conventions(tmp_path)
    assert code == 1
    assert "FAIL bad ci: no workflow in .github/workflows" in report
    assert check_conventions(tmp_path, names=["good"])[1] == 0


def test_unknown_repo_name(tmp_path):
    make_repo(tmp_path, "good")
    with pytest.raises(LookupError):
        check_conventions(tmp_path, names=["nope"])
    assert main(["--root", str(tmp_path), "--repo", "nope"]) == 2


def test_main_prints_report(tmp_path, capsys):
    make_repo(tmp_path, "good")
    assert main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[1].split()[:2] == ["good", "ok"]


def test_main_rejects_missing_root(tmp_path):
    assert main(["--root", str(tmp_path / "missing")]) == 2
