"""Check every sibling repo against the shared conventions; print one table.

Scans the parent directory of this repo (or --root) for git repos and checks
each one against the conventions in workspace/CLAUDE.md:

    readme      README.md exists                                  (every repo)
    python      requires-python is ">=3.14", or pins an exact
                3.14.N with "==" (and .python-version, if present,
                says the same version)                            (Python repos)
    lock        uv.lock exists and is committed                   (Python repos)
    ci          a GitHub Actions workflow in .github/workflows    (Python repos)
    dependabot  .github/dependabot.yml                            (Python repos)
    pre-commit  .pre-commit-config.yaml; a warning, not a failure (Python repos)
    standalone  no import of, or dependency on, a sibling repo's
                package                                     (STANDALONE repos)

A Python repo is one with a pyproject.toml.  Read-only: nothing is fixed or
committed.  Standard library only, so `uv run check_conventions.py` just works.
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
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from check_all import _normalise, dependency_names
from sync_repos import add_root_argument, discover_repos, run_git

CHECKS = ("readme", "python", "lock", "ci", "dependabot", "pre-commit", "standalone")
PYTHON_CHECKS = frozenset(CHECKS) - {"readme", "standalone"}

# Repos that must never import from or depend on a sibling repo (workspace/CLAUDE.md).
STANDALONE = frozenset({"cqf"})

# Folders never scanned for imports.
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "site-packages"})
MAX_HITS = 5

EXACT_PIN = re.compile(r"^==\s*(3\.14\.\d+)$")
FROM_LINE = re.compile(r"^\s*from\s+([A-Za-z_]\w*)")
IMPORT_LINE = re.compile(r"^\s*import\s+([^#;]+)")
MODULE = re.compile(r"[A-Za-z_]\w*")

RED = "31"
GREEN = "32"
YELLOW = "33"
BOLD = "1"


class Status(enum.StrEnum):
    OK = "ok"
    FAIL = "FAIL"
    WARN = "warn"
    NA = "-"


@dataclass
class CheckResult:
    check: str
    status: Status
    detail: str = ""


@dataclass
class RepoReport:
    name: str
    results: list[CheckResult] = field(default_factory=list)

    def get(self, check: str) -> CheckResult | None:
        return next((r for r in self.results if r.check == check), None)

    @property
    def failed(self) -> bool:
        return any(r.status is Status.FAIL for r in self.results)


def read_pyproject(path: Path) -> dict[str, Any] | None:
    """The parsed pyproject.toml, or None if the repo has none.

    Raises ValueError when the file cannot be read or parsed.
    """
    file = path / "pyproject.toml"
    if not file.is_file():
        return None
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read pyproject.toml: {exc}") from exc


def check_readme(path: Path) -> CheckResult:
    if (path / "README.md").is_file():
        return CheckResult("readme", Status.OK)
    return CheckResult("readme", Status.FAIL, "no README.md")


def check_python(path: Path, pyproject: dict[str, Any]) -> CheckResult:
    spec = pyproject.get("project", {}).get("requires-python")
    if not isinstance(spec, str):
        return CheckResult("python", Status.FAIL, "no requires-python in [project]")
    spec = spec.strip()
    if re.fullmatch(r">=\s*3\.14", spec):
        return CheckResult("python", Status.OK)
    match = EXACT_PIN.match(spec)
    if not match:
        return CheckResult(
            "python",
            Status.FAIL,
            f'requires-python is "{spec}"; expected ">=3.14" or "==3.14.N"',
        )
    version_file = path / ".python-version"
    if version_file.is_file():
        pinned = version_file.read_text(encoding="utf-8").strip()
        if pinned != match[1]:
            return CheckResult(
                "python",
                Status.FAIL,
                f'requires-python is "{spec}" but .python-version is "{pinned}"',
            )
    return CheckResult("python", Status.OK)


def check_lock(path: Path) -> CheckResult:
    if not (path / "uv.lock").is_file():
        return CheckResult("lock", Status.FAIL, "no uv.lock")
    done = run_git(path, "ls-files", "--error-unmatch", "--", "uv.lock")
    if done.returncode != 0:
        return CheckResult("lock", Status.FAIL, "uv.lock is not committed")
    return CheckResult("lock", Status.OK)


def check_ci(path: Path) -> CheckResult:
    workflows = path / ".github" / "workflows"
    if workflows.is_dir() and any(
        p.suffix in (".yml", ".yaml") for p in workflows.iterdir() if p.is_file()
    ):
        return CheckResult("ci", Status.OK)
    return CheckResult("ci", Status.FAIL, "no workflow in .github/workflows")


def check_dependabot(path: Path) -> CheckResult:
    github = path / ".github"
    if (github / "dependabot.yml").is_file() or (github / "dependabot.yaml").is_file():
        return CheckResult("dependabot", Status.OK)
    return CheckResult("dependabot", Status.FAIL, "no .github/dependabot.yml")


def check_pre_commit(path: Path) -> CheckResult:
    if (path / ".pre-commit-config.yaml").is_file():
        return CheckResult("pre-commit", Status.OK)
    return CheckResult("pre-commit", Status.WARN, "no .pre-commit-config.yaml")


def sibling_packages(repos: Sequence[Path], exclude: Path) -> dict[str, str]:
    """Importable package names of the other Python repos, mapped to their repo.

    A package is a folder in src/ with an __init__.py, or the project name
    with dashes turned into underscores.
    """
    packages: dict[str, str] = {}
    for repo in repos:
        if repo == exclude or not (repo / "pyproject.toml").is_file():
            continue
        src = repo / "src"
        if src.is_dir():
            for child in src.iterdir():
                if (child / "__init__.py").is_file():
                    packages[child.name] = repo.name
        try:
            name = (read_pyproject(repo) or {}).get("project", {}).get("name")
        except ValueError:
            name = None
        if isinstance(name, str):
            packages[re.sub(r"[-.]+", "_", name).lower()] = repo.name
    return packages


def sibling_projects(repos: Sequence[Path], exclude: Path) -> dict[str, str]:
    """Normalised project names of the other Python repos, mapped to their repo."""
    projects: dict[str, str] = {}
    for repo in repos:
        if repo == exclude:
            continue
        try:
            name = (read_pyproject(repo) or {}).get("project", {}).get("name")
        except ValueError:
            continue
        if isinstance(name, str):
            projects[_normalise(name)] = repo.name
    return projects


def source_files(path: Path) -> Iterator[Path]:
    """The .py and .ipynb files in `path`, skipping hidden and tool folders."""
    for folder, dirs, files in os.walk(path):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS
        )
        for name in sorted(files):
            if name.endswith((".py", ".ipynb")):
                yield Path(folder) / name


def code_lines(file: Path) -> list[str]:
    """The lines of a .py file, or of every code cell of a notebook."""
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if file.suffix != ".ipynb":
        return text.splitlines()
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError:
        return text.splitlines()
    lines: list[str] = []
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        lines += ("".join(source) if isinstance(source, list) else source).splitlines()
    return lines


def imported_modules(line: str) -> list[str]:
    """Top-level module names an import statement on `line` brings in."""
    if match := FROM_LINE.match(line):
        return [match[1]]
    match = IMPORT_LINE.match(line)
    if not match:
        return []
    # "import a.b as x, c" -> each comma part's first dotted name.
    modules = (MODULE.match(part.strip()) for part in match[1].split(","))
    return [m[0] for m in modules if m]


def check_standalone(
    path: Path, pyproject: dict[str, Any] | None, repos: Sequence[Path]
) -> CheckResult:
    packages = sibling_packages(repos, exclude=path)
    projects = sibling_projects(repos, exclude=path)
    hits: list[str] = []
    if pyproject is not None:
        for dep in sorted(dependency_names(pyproject)):
            if dep in projects:
                hits.append(f"pyproject.toml depends on {dep} ({projects[dep]})")
    for file in source_files(path):
        for number, line in enumerate(code_lines(file), start=1):
            for module in imported_modules(line):
                if module in packages:
                    where = file.relative_to(path).as_posix()
                    hits.append(
                        f"{where}:{number} imports {module} ({packages[module]})"
                    )
    if not hits:
        return CheckResult("standalone", Status.OK)
    shown = hits[:MAX_HITS]
    more = len(hits) - len(shown)
    detail = "; ".join(shown) + (f"; and {more} more" if more else "")
    return CheckResult("standalone", Status.FAIL, detail)


def check_repo(path: Path, repos: Sequence[Path]) -> RepoReport:
    report = RepoReport(path.name)
    report.results.append(check_readme(path))
    try:
        pyproject = read_pyproject(path)
    except ValueError as exc:
        report.results.append(CheckResult("python", Status.FAIL, str(exc)))
        pyproject = {}
    if pyproject is None:
        report.results += [
            CheckResult(c, Status.NA) for c in CHECKS if c in PYTHON_CHECKS
        ]
    else:
        if report.get("python") is None:
            report.results.append(check_python(path, pyproject))
        report.results += [
            check_lock(path),
            check_ci(path),
            check_dependabot(path),
            check_pre_commit(path),
        ]
    if path.name in STANDALONE:
        report.results.append(check_standalone(path, pyproject, repos))
    else:
        report.results.append(CheckResult("standalone", Status.NA))
    return report


def render_report(reports: list[RepoReport], color: bool = False) -> str:
    """One row per repo, one column per check, then every failure and warning."""
    paint_for = {Status.OK: GREEN, Status.FAIL: RED, Status.WARN: YELLOW}

    def paint(text: str, code: str | None) -> str:
        return f"\033[{code}m{text}\033[0m" if color and code else text

    name_width = max([len("repo"), *(len(r.name) for r in reports)])
    widths = [max(len(c), len(Status.FAIL)) for c in CHECKS]
    header = "  ".join(
        [
            "repo".ljust(name_width),
            *(c.ljust(w) for c, w in zip(CHECKS, widths, strict=True)),
        ]
    )
    lines = [paint(header.rstrip(), BOLD)]
    for report in reports:
        cells = [report.name.ljust(name_width)]
        for check, width in zip(CHECKS, widths, strict=True):
            result = report.get(check)
            status = result.status if result else Status.NA
            cells.append(paint(str(status).ljust(width), paint_for.get(status)))
        lines.append("  ".join(cells).rstrip())

    problems = [
        (report, result)
        for report in reports
        for result in report.results
        if result.status in (Status.FAIL, Status.WARN)
    ]
    if problems:
        lines.append("")
    for report, result in problems:
        mark = paint(str(result.status), paint_for[result.status])
        lines.append(f"{mark} {report.name} {result.check}: {result.detail}")
    return "\n".join(lines) + "\n"


def check_conventions(
    root: Path, names: Sequence[str] = (), color: bool = False
) -> tuple[str, int]:
    """The report and the exit code: 0 no failures, 1 at least one.

    Raises LookupError when `names` asks for a repo that is not in `root`.
    """
    repos = discover_repos(root)
    chosen = repos
    if names:
        known = {p.name: p for p in repos}
        missing = [n for n in names if n not in known]
        if missing:
            raise LookupError(f"no git repo named {', '.join(missing)} in {root}")
        chosen = [known[n] for n in dict.fromkeys(names)]
    if not chosen:
        return f"No git repos found in {root}\n", 0
    reports = [check_repo(path, repos) for path in chosen]
    return render_report(reports, color), 1 if any(r.failed for r in reports) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every sibling repo against the shared conventions."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NAME",
        help="check only this repo; repeat for more (default: every repo)",
    )
    args = parser.parse_args(argv)

    if shutil.which("git") is None:
        print("check_conventions: git was not found on PATH", file=sys.stderr)
        return 2
    if not args.root.is_dir():
        print(f"check_conventions: {args.root} is not a folder", file=sys.stderr)
        return 2

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        report, code = check_conventions(
            args.root,
            names=args.repo,
            # NO_COLOR applies only when set to a non-empty value.
            color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
        )
    except LookupError as exc:
        print(f"check_conventions: {exc.args[0]}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"check_conventions: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    print(report, end="")
    return code


if __name__ == "__main__":
    sys.exit(main())
