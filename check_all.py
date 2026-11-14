"""Run lint, format, type checks and tests in every sibling repo; print one table.

Scans the parent directory of this repo (or --root) for git repos with a
`pyproject.toml`.  In each one it runs `uv sync --locked`, then these steps
under `uv run --no-sync`, picking each command from the repo's dependencies:

    lint    ruff check .            (if ruff is a dependency)
    format  ruff format --check .   (if ruff is a dependency)
    types   ty check                (if ty is a dependency)
            mypy src | mypy .       (else if mypy is; bare `mypy` when
                                     [tool.mypy] sets `files`)
    tests   pytest                  (if pytest is a dependency and the repo
                                     has tests/ or [tool.pytest] testpaths)

A repo can override or switch off any step in its pyproject.toml:

    [tool.repo-tools.check-all]
    lint = ["ruff", "check", "src", "tests"]
    types = false

Nothing is fixed or committed.  `uv sync --locked` does bring each repo's
.venv in line with its lock file, as `uv run` would.  Standard library only,
so `uv run check_all.py` just works.
"""

import argparse
import enum
import io
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sync_repos import add_root_argument, discover_repos

STEPS = ("lint", "format", "types", "tests")
DEFAULT_TIMEOUT = 900
DEFAULT_TAIL = 20
DEFAULT_JOBS = 4
PYTEST_NO_TESTS = 5

RED = "31"
GREEN = "32"
YELLOW = "33"
BOLD = "1"

Runner = Callable[[Path, Sequence[str], float], subprocess.CompletedProcess[str]]


class Status(enum.StrEnum):
    PASS = "pass"
    FAIL = "FAIL"
    SKIP = "-"  # not applicable to this repo, or switched off
    NOT_RUN = "not run"  # an earlier step (uv sync) failed
    ERROR = "ERROR"  # could not be run: timeout, uv missing, bad config


@dataclass(slots=True)
class StepResult:
    step: str
    status: Status
    command: list[str] = field(default_factory=list)
    detail: str = ""  # why it was skipped, or why it errored
    output: str = ""
    seconds: float = 0.0


@dataclass(slots=True)
class RepoResult:
    path: Path
    results: list[StepResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name

    def get(self, step: str) -> StepResult | None:
        return next((r for r in self.results if r.step == step), None)

    @property
    def failed(self) -> bool:
        return any(r.status is Status.FAIL for r in self.results)

    @property
    def errored(self) -> bool:
        return any(r.status is Status.ERROR for r in self.results)


_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_names(pyproject: dict[str, Any]) -> set[str]:
    """Every package named in dependencies, optional-dependencies and groups."""
    project = pyproject.get("project", {})
    requirements: list[Any] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements += extra
    for group in pyproject.get("dependency-groups", {}).values():
        requirements += group  # {include-group = ...} entries are skipped below
    names = set()
    for requirement in requirements:
        if isinstance(requirement, str) and (
            match := _REQUIREMENT_NAME.match(requirement)
        ):
            names.add(_normalise(match[1]))
    return names


def _has_tests(path: Path, pyproject: dict[str, Any]) -> bool:
    pytest_config = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
    return bool(pytest_config.get("testpaths")) or (path / "tests").is_dir()


def default_command(
    step: str, path: Path, pyproject: dict[str, Any]
) -> list[str] | str:
    """The command for `step`, or the reason it does not apply to this repo."""
    deps = dependency_names(pyproject)
    match step:
        case "lint":
            return (
                ["ruff", "check", "."] if "ruff" in deps else "ruff is not a dependency"
            )
        case "format":
            if "ruff" not in deps:
                return "ruff is not a dependency"
            return ["ruff", "format", "--check", "."]
        case "types":
            if "ty" in deps:
                return ["ty", "check"]
            if "mypy" in deps:
                if pyproject.get("tool", {}).get("mypy", {}).get("files"):
                    return ["mypy"]
                return ["mypy", "src" if (path / "src").is_dir() else "."]
            return "no type checker (ty or mypy) is a dependency"
        case "tests":
            if "pytest" not in deps:
                return "pytest is not a dependency"
            if not _has_tests(path, pyproject):
                return "no tests/ directory or testpaths"
            return ["pytest"]
    raise ValueError(f"unknown step {step!r}")


def plan(path: Path, pyproject: dict[str, Any], step: str) -> list[str] | str:
    """Command for `step` after any [tool.repo-tools.check-all] override.

    Returns a reason string when the step is skipped.  Raises ValueError for an
    override that is neither false nor a non-empty list of strings.
    """
    overrides = pyproject.get("tool", {}).get("repo-tools", {}).get("check-all", {})
    if step not in overrides:
        return default_command(step, path, pyproject)
    value = overrides[step]
    if value is False:
        return "switched off in [tool.repo-tools.check-all]"
    if (
        isinstance(value, list)
        and value
        and all(isinstance(part, str) and part for part in value)
    ):
        return list(value)
    raise ValueError(
        f"[tool.repo-tools.check-all] {step} must be false or a list of strings"
    )


def child_env() -> dict[str, str]:
    """Environment for uv in another repo.

    VIRTUAL_ENV is dropped: when this script runs under `uv run` it points at
    repo-tools' own .venv, which uv would otherwise warn about in every repo.
    Colour is off because the output is captured.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("VIRTUAL_ENV", "FORCE_COLOR", "CLICOLOR_FORCE")
    }
    env["NO_COLOR"] = "1"
    return env


def run_uv(
    path: Path, args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run uv in `path` with no way to prompt, capturing stdout and stderr together."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["uv", *args],  # noqa: S607 - uv is looked up on PATH on purpose
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        timeout=timeout,
        env=child_env(),
    )


def _execute(
    step: str, uv_args: list[str], path: Path, run: Runner, timeout: float
) -> StepResult:
    start = time.monotonic()
    result = StepResult(step, Status.PASS, command=["uv", *uv_args])
    try:
        done = run(path, uv_args, timeout)
    except subprocess.TimeoutExpired as exc:
        result.status, result.detail = Status.ERROR, f"timed out after {timeout:g}s"
        partial = exc.stdout or b""
        result.output = (
            partial.decode("utf-8", "replace")
            if isinstance(partial, bytes)
            else partial
        )
    except OSError as exc:
        result.status, result.detail = Status.ERROR, str(exc)
    else:
        result.output = done.stdout or ""
        if step == "tests" and done.returncode == PYTEST_NO_TESTS:
            result.status, result.detail = Status.SKIP, "pytest collected no tests"
        elif done.returncode != 0:
            result.status, result.detail = Status.FAIL, f"exit status {done.returncode}"
    result.seconds = time.monotonic() - start
    return result


def check_repo(
    path: Path,
    steps: Sequence[str] = STEPS,
    run: Runner = run_uv,
    timeout: float = DEFAULT_TIMEOUT,
) -> RepoResult:
    """Sync `path`, then run each step.  Never raises for a misbehaving repo."""
    start = time.monotonic()
    repo = RepoResult(path)
    try:
        pyproject = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        repo.results.append(
            StepResult("sync", Status.ERROR, detail=f"pyproject.toml: {exc}")
        )
        repo.results += [StepResult(s, Status.NOT_RUN) for s in steps]
        return repo

    sync = _execute("sync", ["sync", "--locked"], path, run, timeout)
    repo.results.append(sync)
    for step in steps:
        if sync.status is not Status.PASS:
            repo.results.append(
                StepResult(step, Status.NOT_RUN, detail="uv sync failed")
            )
            continue
        try:
            command = plan(path, pyproject, step)
        except ValueError as exc:
            repo.results.append(StepResult(step, Status.ERROR, detail=str(exc)))
            continue
        if isinstance(command, str):
            repo.results.append(StepResult(step, Status.SKIP, detail=command))
            continue
        repo.results.append(
            _execute(step, ["run", "--no-sync", *command], path, run, timeout)
        )
    repo.seconds = time.monotonic() - start
    return repo


def python_repos(root: Path, names: Sequence[str] = ()) -> tuple[list[Path], list[str]]:
    """Repos with a pyproject.toml, and the names of those without one.

    Raises LookupError when `names` asks for a repo that is not in `root`.
    """
    repos = discover_repos(root)
    if names:
        known = {p.name: p for p in repos}
        missing = [n for n in names if n not in known]
        if missing:
            raise LookupError(f"no git repo named {', '.join(missing)} in {root}")
        repos = [known[n] for n in dict.fromkeys(names)]
    with_project = [p for p in repos if (p / "pyproject.toml").is_file()]
    without = [p.name for p in repos if not (p / "pyproject.toml").is_file()]
    return with_project, without


def _tail(text: str, lines: int) -> list[str]:
    body = text.rstrip().splitlines()
    return body[-lines:] if lines else body


def render_report(
    repos: list[RepoResult],
    steps: Sequence[str] = STEPS,
    without_project: Sequence[str] = (),
    color: bool = False,
    tail: int = DEFAULT_TAIL,
) -> str:
    """One row per repo, then the output of every failed or errored step."""
    columns = ["sync", *steps]
    paint_for = {Status.PASS: GREEN, Status.FAIL: RED, Status.ERROR: RED}

    def paint(text: str, code: str | None) -> str:
        return f"\033[{code}m{text}\033[0m" if color and code else text

    name_width = max([len("repo"), *(len(r.name) for r in repos)])
    widths = [max(len(c), len(Status.NOT_RUN)) for c in columns]
    header = "  ".join(
        [
            "repo".ljust(name_width),
            *(c.ljust(w) for c, w in zip(columns, widths, strict=True)),
            "time",
        ]
    )
    lines = [paint(header, BOLD)]
    for repo in repos:
        cells = [repo.name.ljust(name_width)]
        for column, width in zip(columns, widths, strict=True):
            result = repo.get(column)
            status = result.status if result else Status.SKIP
            cells.append(paint(str(status).ljust(width), paint_for.get(status)))
        cells.append(f"{repo.seconds:.0f}s")
        lines.append("  ".join(cells).rstrip())

    problems = [
        (repo, r)
        for repo in repos
        for r in repo.results
        if r.status in (Status.FAIL, Status.ERROR)
    ]
    for repo, result in problems:
        title = f"{repo.name} {result.step}: {result.status} ({result.detail})"
        lines += ["", paint(title, RED if color else None)]
        if result.command:
            lines.append(f"  $ {' '.join(result.command)}")
        lines += [f"  {line}" for line in _tail(result.output, tail)]

    skipped = [
        f"{repo.name} {r.step}: {r.detail}"
        for repo in repos
        for r in repo.results
        if r.status is Status.SKIP and r.detail
    ]
    if skipped:
        lines += ["", "Skipped:", *(f"  {s}" for s in skipped)]
    if without_project:
        lines += ["", f"No pyproject.toml: {', '.join(without_project)}"]
    return "\n".join(lines) + "\n"


def check_all(
    root: Path,
    *,
    names: Sequence[str] = (),
    steps: Sequence[str] = STEPS,
    jobs: int = DEFAULT_JOBS,
    timeout: float = DEFAULT_TIMEOUT,
    tail: int = DEFAULT_TAIL,
    color: bool = False,
    run: Runner = run_uv,
    out: Callable[[str], None] = lambda text: print(text, end=""),
    progress: Callable[[str], None] | None = None,
) -> int:
    """Check every repo and print the report; returns the process exit code."""
    repos, without = python_repos(root, names)
    if not repos:
        out(f"No Python repos found in {root}\n")
        return 0

    def one(path: Path) -> RepoResult:
        result = check_repo(path, steps, run, timeout)
        if progress:
            verdict = (
                "errors" if result.errored else "failed" if result.failed else "ok"
            )
            progress(f"{result.name}: {verdict} ({result.seconds:.0f}s)\n")
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(repos)))) as pool:
        results = list(pool.map(one, repos))

    out(render_report(results, steps, without, color, tail))
    if any(r.errored for r in results):
        return 2
    return 1 if any(r.failed for r in results) else 0


def step_list(text: str) -> list[str]:
    """argparse type: a comma-separated subset of STEPS, kept in STEPS order."""
    chosen = {part.strip() for part in text.split(",") if part.strip()}
    unknown = chosen - set(STEPS)
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(
            f"choose from {', '.join(STEPS)}, separated by commas"
        )
    return [s for s in STEPS if s in chosen]


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return value


def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run lint, format, type checks and tests in every sibling repo."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NAME",
        help="check only this repo; repeat for more (default: every repo)",
    )
    parser.add_argument(
        "--steps",
        type=step_list,
        default=list(STEPS),
        help=f"comma-separated steps to run (default: {','.join(STEPS)})",
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=DEFAULT_JOBS,
        help=f"repos checked at once (default: {DEFAULT_JOBS}); "
        "steps within a repo always run one after another",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds allowed per step (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--tail",
        type=non_negative_int,
        default=DEFAULT_TAIL,
        help=f"output lines shown per failed step, 0 for all (default: {DEFAULT_TAIL})",
    )
    args = parser.parse_args(argv)

    if shutil.which("uv") is None:
        print("check_all: uv was not found on PATH", file=sys.stderr)
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        return check_all(
            args.root,
            names=args.repo,
            steps=args.steps,
            jobs=args.jobs,
            timeout=args.timeout,
            tail=args.tail,
            # NO_COLOR applies only when set to a non-empty value.
            color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
            progress=(
                (lambda text: print(text, end="", file=sys.stderr, flush=True))
                if sys.stderr.isatty()
                else None
            ),
        )
    except LookupError as exc:
        print(f"check_all: {exc.args[0]}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
