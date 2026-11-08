from pathlib import Path

import pytest

from install_workspace import HEADER, SOURCE, install, main, rendered


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
