import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from install_workspace import (
    HEADER,
    MARKETPLACE,
    MARKETPLACE_SOURCE,
    PLUGIN,
    SOURCE,
    install,
    main,
    rendered,
    setup_plugin,
)

INSTALLED = {"id": PLUGIN, "version": "0.1.1", "scope": "user", "enabled": True}
LIST_MARKETPLACES = ("plugin", "marketplace", "list", "--json")
LIST_PLUGINS = ("plugin", "list", "--json")


class FakeClaude:
    """Stands in for the claude CLI: canned list output, records every call."""

    def __init__(
        self,
        *,
        marketplace: bool = True,
        plugin: dict | None = INSTALLED,
        fail: tuple[str, ...] | None = None,
    ):
        self.marketplaces = [{"name": MARKETPLACE}] if marketplace else []
        self.plugins = [plugin] if plugin else []
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = tuple(args)
        self.calls.append(call)
        if call == self.fail:
            return subprocess.CompletedProcess(call, 1, "", "boom\n")
        if call == LIST_MARKETPLACES:
            return subprocess.CompletedProcess(call, 0, json.dumps(self.marketplaces))
        if call == LIST_PLUGINS:
            return subprocess.CompletedProcess(call, 0, json.dumps(self.plugins))
        return subprocess.CompletedProcess(call, 0, "ok\n", "")

    @property
    def changes(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c not in (LIST_MARKETPLACES, LIST_PLUGINS)]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.md"
    path.write_text("# Shared guidance\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repos"
    path.mkdir()
    return path


def test_real_source_exists():
    assert SOURCE.is_file()


def test_rendered_starts_with_header(source):
    assert rendered(source) == HEADER + "# Shared guidance\n"


def test_writes_missing_copy(root, source):
    assert install(root, check=False, source=source) == 0
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == rendered(source)
    assert not (root / "CLAUDE.md.bak").exists()


def test_up_to_date_copy_is_left_alone(root, source):
    install(root, check=False, source=source)
    assert install(root, check=True, source=source) == 0
    assert install(root, check=False, source=source) == 0
    assert not (root / "CLAUDE.md.bak").exists()


def test_check_reports_missing_and_stale_without_writing(root, source):
    assert install(root, check=True, source=source) == 1
    assert not (root / "CLAUDE.md").exists()

    (root / "CLAUDE.md").write_text("edited by hand\n", encoding="utf-8")
    assert install(root, check=True, source=source) == 1
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "edited by hand\n"


def test_replacing_a_stale_copy_backs_it_up(root, source):
    (root / "CLAUDE.md").write_text("edited by hand\n", encoding="utf-8")
    assert install(root, check=False, source=source) == 0
    assert (root / "CLAUDE.md.bak").read_text(encoding="utf-8") == "edited by hand\n"
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == rendered(source)


def test_main_rejects_missing_root(tmp_path):
    assert main(["--root", str(tmp_path / "nope")]) == 2


ADD = ("plugin", "marketplace", "add", MARKETPLACE_SOURCE)
INSTALL = ("plugin", "install", PLUGIN, "--scope", "user")
UPDATE_MARKETPLACE = ("plugin", "marketplace", "update", MARKETPLACE)
UPDATE_PLUGIN = ("plugin", "update", PLUGIN)


def test_plugin_already_installed_changes_nothing(capsys):
    claude = FakeClaude()
    assert setup_plugin(claude, check=False, update=False) == 0
    assert claude.changes == []
    assert "0.1.1 is installed (user scope)" in capsys.readouterr().out


def test_missing_marketplace_is_added_then_plugin_installed():
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=False, update=False) == 0
    assert claude.changes == [ADD, INSTALL]


def test_missing_plugin_only_is_installed():
    claude = FakeClaude(plugin=None)
    assert setup_plugin(claude, check=False, update=False) == 0
    assert claude.changes == [INSTALL]


def test_disabled_plugin_is_reported_not_enabled(capsys):
    claude = FakeClaude(plugin={**INSTALLED, "enabled": False})
    assert setup_plugin(claude, check=False, update=False) == 0
    assert claude.changes == []
    assert "disabled" in capsys.readouterr().out


def test_check_reports_missing_without_changing_anything(capsys):
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=True, update=True) == 1
    assert claude.changes == []
    assert f"marketplace {MARKETPLACE} and plugin {PLUGIN}" in capsys.readouterr().out


def test_check_passes_when_installed():
    claude = FakeClaude()
    assert setup_plugin(claude, check=True, update=False) == 0
    assert claude.changes == []


def test_update_updates_marketplace_then_plugin():
    claude = FakeClaude()
    assert setup_plugin(claude, check=False, update=True) == 0
    assert claude.changes == [UPDATE_MARKETPLACE, UPDATE_PLUGIN]


def test_fresh_install_with_update_does_not_also_update():
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=False, update=True) == 0
    assert claude.changes == [ADD, INSTALL]


def test_cli_failure_exits_2(capsys):
    claude = FakeClaude(plugin=None, fail=INSTALL)
    assert setup_plugin(claude, check=False, update=False) == 2
    assert "boom" in capsys.readouterr().err


def test_bad_json_exits_2():
    def claude(args):
        return subprocess.CompletedProcess(args, 0, "not json", "")

    assert setup_plugin(claude, check=False, update=False) == 2


def test_missing_cli_is_skipped(capsys):
    assert setup_plugin(None, check=True, update=False) == 0
    assert "not on PATH" in capsys.readouterr().out


def test_main_no_plugin_skips_cli(root, monkeypatch):
    monkeypatch.setattr("install_workspace.shutil.which", lambda _: pytest.fail())
    assert main(["--root", str(root), "--no-plugin"]) == 0
