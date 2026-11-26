"""build_version.py: the package version from `git describe` (it lives outside the package)."""

import importlib.util
import re
import subprocess
from importlib.metadata import version
from pathlib import Path

import pytest

import fpgas_online_mcp

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_version", ROOT / "build_version.py")
bv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bv)


def test_from_describe():
    assert bv.from_describe("v0.0-80-g29a5a1a\n") == "0.0.80"
    assert bv.from_describe("v1.12-0-gabcdef0") == "1.12.0"
    assert bv.from_describe("v0.0-81-g29a5a1a-dirty") == "0.0.81+dirty"


@pytest.mark.parametrize("bad", ["29a5a1a", "v0.0", "v0.0.1-3-gabc", "release-3-gabc", ""])
def test_from_describe_refuses_other_shapes(bad):
    with pytest.raises(ValueError):
        bv.from_describe(bad)


def test_sdist_uses_pkg_info(tmp_path):
    (tmp_path / "PKG-INFO").write_text("Metadata-Version: 2.4\nName: fpgas-online-mcp\nVersion: 0.0.42\n")
    assert bv.get_version(tmp_path) == "0.0.42"


def test_no_git_history_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="git describe"):
        bv.get_version(tmp_path)


def test_checkout_version_is_git_describe():
    out = subprocess.run(
        ["git", "-C", str(ROOT), "describe", "--tags", "--long", "--match", "v[0-9]*", "--dirty"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert bv.get_version(ROOT) == bv.from_describe(out)


def test_installed_version_matches_metadata():
    assert fpgas_online_mcp.__version__ == version("fpgas-online-mcp")
    assert re.fullmatch(r"\d+\.\d+\.\d+(\+dirty)?", fpgas_online_mcp.__version__)
