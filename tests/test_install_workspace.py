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
    PLUGIN_MANIFEST,
    PRIVATE,
    SOURCE,
    install,
    local_version,
    main,
    rendered,
    setup_plugin,
)

LOCAL = "0.1.1"
INSTALLED = {"id": PLUGIN, "version": LOCAL, "scope": "user", "enabled": True}
STALE = {**INSTALLED, "version": "0.1.0"}
NEWER = {**INSTALLED, "version": "0.2.0"}
LIST_MARKETPLACES = ("plugin", "marketplace", "list", "--json")
LIST_PLUGINS = ("plugin", "list", "--json")


class FakeClaude:
    """Stands in for the claude CLI: canned list output, records every call."""

    def __init__(
        self,
        *,
        marketplace: bool = True,
        plugin: dict | None = INSTALLED,
        after: list[dict] | None = None,
        fail: tuple[str, ...] | None = None,
    ):
        """after is what plugin list shows once install or update has run.

        By default an install lists INSTALLED and an update changes nothing.
        """
        self.marketplaces = [{"name": MARKETPLACE}] if marketplace else []
        self.plugins = [plugin] if plugin else []
        self.after = after
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = tuple(args)
        self.calls.append(call)
        if call == self.fail:
            return subprocess.CompletedProcess(call, 1, "", "boom\n")
        if call[:2] == ("plugin", "install"):
            self.plugins = [INSTALLED] if self.after is None else self.after
        elif call[:2] == ("plugin", "update") and self.after is not None:
            self.plugins = self.after
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


def test_private_guidance_is_appended(root, source):
    (root / PRIVATE).write_text("\n## Private repos\n\n- notes\n\n", encoding="utf-8")
    assert install(root, check=False, source=source) == 0
    copy = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert copy == (
        HEADER
        + "# Shared guidance\n\n"
        + f"<!-- Appended from {PRIVATE}, which is not in git. -->\n\n"
        + "## Private repos\n\n- notes\n"
    )
    assert install(root, check=True, source=source) == 0


def test_changed_private_guidance_makes_the_copy_stale(root, source):
    (root / PRIVATE).write_text("- notes\n", encoding="utf-8")
    install(root, check=False, source=source)
    (root / PRIVATE).write_text("- more notes\n", encoding="utf-8")
    assert install(root, check=True, source=source) == 1
    (root / PRIVATE).unlink()
    assert install(root, check=True, source=source) == 1
    assert install(root, check=False, source=source) == 0
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == rendered(source)


def test_empty_private_guidance_adds_nothing(root, source):
    (root / PRIVATE).write_text("  \n", encoding="utf-8")
    assert rendered(source, root / PRIVATE) == rendered(source)


def test_main_rejects_missing_root(tmp_path):
    assert main(["--root", str(tmp_path / "nope")]) == 2


ADD = ("plugin", "marketplace", "add", MARKETPLACE_SOURCE)
INSTALL = ("plugin", "install", PLUGIN, "--scope", "user")
UPDATE_MARKETPLACE = ("plugin", "marketplace", "update", MARKETPLACE)
UPDATE_PLUGIN = ("plugin", "update", PLUGIN)


def test_plugin_already_installed_changes_nothing(capsys):
    claude = FakeClaude()
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 0
    assert claude.changes == []
    assert "0.1.1 is installed (user scope)" in capsys.readouterr().out


def test_missing_marketplace_is_added_then_plugin_installed():
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 0
    assert claude.changes == [ADD, INSTALL]


def test_missing_plugin_only_is_installed():
    claude = FakeClaude(plugin=None)
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 0
    assert claude.changes == [INSTALL]


def test_disabled_plugin_is_reported_not_enabled(capsys):
    claude = FakeClaude(plugin={**INSTALLED, "enabled": False})
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 0
    assert claude.changes == []
    assert "disabled" in capsys.readouterr().out


def test_check_reports_missing_without_changing_anything(capsys):
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=True, update=True, local=LOCAL) == 1
    assert claude.changes == []
    assert f"marketplace {MARKETPLACE} and plugin {PLUGIN}" in capsys.readouterr().out


def test_check_passes_when_installed():
    claude = FakeClaude()
    assert setup_plugin(claude, check=True, update=False, local=LOCAL) == 0
    assert claude.changes == []


def test_update_updates_marketplace_then_plugin():
    claude = FakeClaude()
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 0
    assert claude.changes == [UPDATE_MARKETPLACE, UPDATE_PLUGIN]


def test_fresh_install_with_update_does_not_also_update():
    claude = FakeClaude(marketplace=False, plugin=None)
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 0
    assert claude.changes == [ADD, INSTALL]


def test_cli_failure_exits_2(capsys):
    claude = FakeClaude(plugin=None, fail=INSTALL)
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 2
    assert "boom" in capsys.readouterr().err


def test_bad_json_exits_2():
    def claude(args):
        return subprocess.CompletedProcess(args, 0, "not json", "")

    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 2


def test_missing_cli_is_skipped(capsys):
    assert setup_plugin(None, check=True, update=False, local=LOCAL) == 0
    assert "not on PATH" in capsys.readouterr().out


def test_main_no_plugin_skips_cli(root, monkeypatch):
    monkeypatch.setattr("install_workspace.shutil.which", lambda _: pytest.fail())
    assert main(["--root", str(root), "--no-plugin"]) == 0


def test_real_manifest_has_version():
    assert local_version(PLUGIN_MANIFEST)


def test_local_version_reads_manifest(tmp_path):
    manifest = tmp_path / "plugin.json"
    manifest.write_text(json.dumps({"name": "x", "version": "1.2.3"}), "utf-8")
    assert local_version(manifest) == "1.2.3"


def test_local_version_missing_or_bad_manifest(tmp_path):
    assert local_version(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", "utf-8")
    assert local_version(bad) is None


def test_check_fails_on_stale_version(capsys):
    claude = FakeClaude(plugin=STALE)
    assert setup_plugin(claude, check=True, update=False, local=LOCAL) == 1
    assert claude.changes == []
    out = capsys.readouterr().out
    assert "0.1.0 is installed but this checkout has 0.1.1" in out
    assert "--update" in out


def test_check_without_local_version_skips_comparison(capsys):
    claude = FakeClaude(plugin=STALE)
    assert setup_plugin(claude, check=True, update=False, local=None) == 0
    assert "skipped the check" in capsys.readouterr().out


def test_default_run_hints_at_stale_version_without_failing(capsys):
    claude = FakeClaude(plugin=STALE)
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 0
    assert claude.changes == []
    assert "run install_workspace.py --update" in capsys.readouterr().out


def test_update_reports_new_version(capsys):
    claude = FakeClaude(plugin=STALE, after=[INSTALLED])
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 0
    assert claude.changes == [UPDATE_MARKETPLACE, UPDATE_PLUGIN]
    assert f"Updated {PLUGIN} 0.1.0 -> 0.1.1" in capsys.readouterr().out


def test_update_already_current(capsys):
    claude = FakeClaude()
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 0
    assert f"{PLUGIN} is already at 0.1.1" in capsys.readouterr().out


def test_update_that_stays_stale_fails(capsys):
    claude = FakeClaude(plugin=STALE)
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 1
    out = capsys.readouterr().out
    assert "already at 0.1.0" in out
    assert f"pushed to {MARKETPLACE_SOURCE}" in out


def test_installed_newer_than_checkout_suggests_pull(capsys):
    claude = FakeClaude(plugin=NEWER)
    assert setup_plugin(claude, check=True, update=False, local=LOCAL) == 0
    assert "pull repo-tools" in capsys.readouterr().out


def test_versions_compare_numerically(capsys):
    claude = FakeClaude(plugin={**INSTALLED, "version": "0.10.0"})
    assert setup_plugin(claude, check=True, update=False, local="0.9.0") == 0
    assert "pull repo-tools" in capsys.readouterr().out


def test_fresh_install_that_is_not_listed_exits_2(capsys):
    claude = FakeClaude(plugin=None, after=[])
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 2
    assert "not listed after installing" in capsys.readouterr().err


def test_fresh_install_older_than_checkout_warns(capsys):
    claude = FakeClaude(plugin=None, after=[STALE])
    assert setup_plugin(claude, check=False, update=False, local=LOCAL) == 1
    assert "pushed to" in capsys.readouterr().out


def test_update_that_removes_plugin_exits_2(capsys):
    claude = FakeClaude(after=[])
    assert setup_plugin(claude, check=False, update=True, local=LOCAL) == 2
    assert "not listed after updating" in capsys.readouterr().err
