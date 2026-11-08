"""Set up this machine for the sibling repos: workspace CLAUDE.md and shared skills.

1. Copies workspace/CLAUDE.md into the folder that holds the sibling repos (the
   parent of repo-tools, or --root).  Claude Code reads CLAUDE.md files from
   parent folders, so the copy gives every repo the shared guidance.  It is a
   plain copy rather than an @import, because Claude Code asks for approval of
   an import from outside the project once per repo.  A copy that differs from
   what would be written is backed up to CLAUDE.md.bak before being replaced.
2. Makes sure the koysor-skills plugin from this repo's marketplace is installed
   at user scope, adding the marketplace and installing the plugin if they are
   missing (and, with --update, updating both).  Skipped if `claude` is not on
   PATH.

Standard library only.
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sync_repos import add_root_argument

SOURCE = Path(__file__).resolve().parent / "workspace" / "CLAUDE.md"
HEADER = (
    "<!-- Copied from repo-tools/workspace/CLAUDE.md by repo-tools/"
    "install_workspace.py. Edit that file, not this copy. -->\n\n"
)

MARKETPLACE_SOURCE = "koysor/repo-tools"
MARKETPLACE = "repo-tools"
PLUGIN = "koysor-skills@repo-tools"
CLAUDE_TIMEOUT = 180

ClaudeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ClaudeError(Exception):
    """A claude CLI command failed."""


def rendered(source: Path = SOURCE) -> str:
    """Return the text the copy in the repos folder should contain."""
    return HEADER + source.read_text(encoding="utf-8")


def install(root: Path, *, check: bool, source: Path = SOURCE) -> int:
    """Write (or with check, compare) root/CLAUDE.md; return the exit code."""
    target = root / "CLAUDE.md"
    wanted = rendered(source)
    current = target.read_text(encoding="utf-8") if target.exists() else None

    if current == wanted:
        print(f"{target} is up to date")
        return 0
    if check:
        state = "missing" if current is None else "out of date"
        print(f"{target} is {state}; run install_workspace.py to update it")
        return 1

    if current is not None:
        backup = target.with_name("CLAUDE.md.bak")
        shutil.copyfile(target, backup)
        print(f"Backed up the old copy to {backup}")
    target.write_text(wanted, encoding="utf-8", newline="\n")
    print(f"Wrote {target} from {source}")
    return 0


def claude_runner(executable: str) -> ClaudeRunner:
    """Return a runner for the claude CLI at executable, which never prompts."""

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=CLAUDE_TIMEOUT,
        )

    return run


def _last_line(done: subprocess.CompletedProcess[str]) -> str:
    lines = (done.stderr or done.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit status {done.returncode}"


def _invoke(claude: ClaudeRunner, args: Sequence[str]) -> str:
    """Run a claude command; return its stdout or raise ClaudeError."""
    try:
        done = claude(args)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClaudeError(f"claude {' '.join(args)}: {error}") from error
    if done.returncode != 0:
        raise ClaudeError(f"claude {' '.join(args)}: {_last_line(done)}")
    return done.stdout


def _run(claude: ClaudeRunner, args: Sequence[str]) -> str:
    """Run a claude command; return its last output line."""
    lines = _invoke(claude, args).strip().splitlines()
    return lines[-1] if lines else "done"


def _json(claude: ClaudeRunner, args: Sequence[str]) -> list[dict[str, Any]]:
    """Run a claude --json command; return the list it prints."""
    stdout = _invoke(claude, args)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ClaudeError(f"claude {' '.join(args)}: not JSON ({error})") from error
    return data if isinstance(data, list) else []


def setup_plugin(claude: ClaudeRunner | None, *, check: bool, update: bool) -> int:
    """Make sure the shared skills plugin is installed; return the exit code."""
    if claude is None:
        print("claude is not on PATH; skipping the koysor-skills plugin setup")
        return 0
    try:
        marketplaces = _json(claude, ["plugin", "marketplace", "list", "--json"])
        has_marketplace = any(m.get("name") == MARKETPLACE for m in marketplaces)
        plugins = _json(claude, ["plugin", "list", "--json"])
        plugin = next((p for p in plugins if p.get("id") == PLUGIN), None)

        if check:
            missing = [
                name
                for name, present in (
                    (f"marketplace {MARKETPLACE}", has_marketplace),
                    (f"plugin {PLUGIN}", plugin is not None),
                )
                if not present
            ]
            if missing:
                print(
                    f"Missing {' and '.join(missing)}; "
                    "run install_workspace.py to install"
                )
                return 1
            _report_installed(plugin)
            return 0

        if not has_marketplace:
            print(f"Adding marketplace {MARKETPLACE} from {MARKETPLACE_SOURCE}")
            print(_run(claude, ["plugin", "marketplace", "add", MARKETPLACE_SOURCE]))
        elif update:
            print(_run(claude, ["plugin", "marketplace", "update", MARKETPLACE]))

        if plugin is None:
            print(f"Installing {PLUGIN} at user scope")
            print(_run(claude, ["plugin", "install", PLUGIN, "--scope", "user"]))
            return 0
        if update:
            print(_run(claude, ["plugin", "update", PLUGIN]))
        else:
            _report_installed(plugin)
        return 0
    except ClaudeError as error:
        print(f"install_workspace: {error}", file=sys.stderr)
        return 2


def _report_installed(plugin: dict[str, Any] | None) -> None:
    if plugin is None:
        return
    version = plugin.get("version", "?")
    scope = plugin.get("scope", "?")
    if plugin.get("enabled", True):
        print(f"{PLUGIN} {version} is installed ({scope} scope)")
    else:
        print(
            f"{PLUGIN} {version} is installed ({scope} scope) but disabled; "
            f"enable it with: claude plugin enable {PLUGIN}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up this machine: copy workspace/CLAUDE.md into the folder "
        "holding the repos and install the koysor-skills plugin."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only; exit 1 if the copy or the plugin is missing or out of date",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="also update the repo-tools marketplace and the koysor-skills plugin",
    )
    parser.add_argument(
        "--no-plugin", action="store_true", help="skip the plugin setup"
    )
    args = parser.parse_args(argv)
    # The claude CLI prints symbols such as a check mark, which a cp1252
    # console (Windows, piped output) cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not args.root.is_dir():
        print(f"install_workspace: {args.root} is not a directory", file=sys.stderr)
        return 2

    status = install(args.root, check=args.check)
    if not args.no_plugin:
        executable = shutil.which("claude")
        claude = claude_runner(executable) if executable else None
        status = max(status, setup_plugin(claude, check=args.check, update=args.update))
    return status


if __name__ == "__main__":
    sys.exit(main())
