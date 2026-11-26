"""XDG directory helpers."""

import shutil
import stat
import tempfile
from pathlib import Path

import pytest

import fpgas_online_mcp
from fpgas_online_mcp import paths

XDG = ("XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME")
REPO_TMP = Path(__file__).resolve().parent.parent / "tmp"


@pytest.fixture
def tmp_path():
    """Short (pytest's own tmp_path is too long for the socket-path check)."""
    REPO_TMP.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=REPO_TMP, prefix="p"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in XDG:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_runtime_dir_uses_xdg_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    assert paths.runtime_dir() == tmp_path / "run" / "fpgas-online"


def test_runtime_dir_fallback_without_xdg_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert paths.runtime_dir() == tmp_path / "cache" / "fpgas-online" / "run"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert paths.runtime_dir() == tmp_path / "home" / ".cache" / "fpgas-online" / "run"


def test_other_dirs_follow_xdg(monkeypatch, tmp_path):
    for name, fn in [
        ("XDG_STATE_HOME", paths.state_dir),
        ("XDG_CACHE_HOME", paths.cache_dir),
        ("XDG_CONFIG_HOME", paths.config_dir),
    ]:
        monkeypatch.setenv(name, str(tmp_path / name))
        assert fn() == tmp_path / name / "fpgas-online"


def test_other_dirs_default_under_home(tmp_path):
    home = tmp_path / "home"
    assert paths.state_dir() == home / ".local" / "state" / "fpgas-online"
    assert paths.cache_dir() == home / ".cache" / "fpgas-online"
    assert paths.config_dir() == home / ".config" / "fpgas-online"


@pytest.mark.parametrize("fn", [paths.runtime_dir, paths.state_dir, paths.cache_dir, paths.config_dir])
def test_dirs_are_mode_0700(fn, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    path = fn()
    assert path.is_dir()
    assert mode(path) == 0o700
    path.chmod(0o755)
    assert mode(fn()) == 0o700


def test_fallback_runtime_dir_and_parent_are_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = paths.runtime_dir()
    assert mode(path) == 0o700
    assert mode(path.parent) == 0o700


def test_socket_path_short(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)
    monkeypatch.setattr(Path, "chmod", lambda *a, **k: None)
    assert len(bytes(paths.runtime_dir() / "broker.sock")) < 100


def test_version_matches_package_metadata():
    from importlib.metadata import version

    assert fpgas_online_mcp.__version__ == "0.1.1"
    assert version("fpgas-online-mcp") == fpgas_online_mcp.__version__


def test_runtime_dir_too_long_for_the_broker_socket(monkeypatch, tmp_path):
    long = tmp_path / ("x" * (110 - len(str(tmp_path))))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long))
    with pytest.raises(paths.PathTooLong, match="broker.sock.*shorter directory"):
        paths.runtime_dir()
    assert not long.exists()


def test_control_path_room():
    paths.check_control_path_room(Path("/run/user/1000/fpgas-online"))
    with pytest.raises(paths.PathTooLong, match="cm-"):
        paths.check_control_path_room(Path("/" + "d" * 60))


async def test_client_reports_too_long_path(monkeypatch, tmp_path):
    from fpgas_online_mcp import client

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("x" * 100)))
    with pytest.raises(client.BrokerError) as e:
        await client.connect("h")
    assert e.value.code == "path_too_long" and "XDG_RUNTIME_DIR" in e.value.message
