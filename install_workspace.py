"""Copy workspace/CLAUDE.md into the folder that holds the sibling repos.

Claude Code reads CLAUDE.md files from parent folders, so a copy in the repos
folder (the parent of repo-tools, or --root) gives every repo the shared
guidance.  The copy is a plain file rather than an @import, because Claude Code
asks for approval of an import from outside the project once per repo.

The source of truth is workspace/CLAUDE.md in this repo; the copy starts with a
comment saying so.  A copy that differs from what would be written is backed up
to CLAUDE.md.bak before being replaced.  Standard library only.
"""

import argparse
import shutil
import sys
from pathlib import Path

from sync_repos import add_root_argument

SOURCE = Path(__file__).resolve().parent / "workspace" / "CLAUDE.md"
HEADER = (
    "<!-- Copied from repo-tools/workspace/CLAUDE.md by repo-tools/"
    "install_workspace.py. Edit that file, not this copy. -->\n\n"
)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy workspace/CLAUDE.md into the folder holding the repos."
    )
    add_root_argument(parser)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only; exit 1 if the copy is missing or out of date",
    )
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"install_workspace: {args.root} is not a directory", file=sys.stderr)
        return 2
    return install(args.root, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
