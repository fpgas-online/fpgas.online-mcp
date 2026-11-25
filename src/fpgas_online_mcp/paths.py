"""XDG directories used by the broker, front end and CLI. Each is created mode 0700."""

import os
from pathlib import Path

APP = "fpgas-online"


def _xdg(name: str, default: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else Path.home() / default


def _private(path: Path, top: Path) -> Path:
    """Create path (and parents below top) mode 0700, tightening existing directories."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (path, *path.parents):
        d.chmod(0o700)
        if d == top:
            break
    return path


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        top = Path(runtime) / APP
        return _private(top, top)
    top = _xdg("XDG_CACHE_HOME", ".cache") / APP
    return _private(top / "run", top)


def state_dir() -> Path:
    path = _xdg("XDG_STATE_HOME", ".local/state") / APP
    return _private(path, path)


def cache_dir() -> Path:
    path = _xdg("XDG_CACHE_HOME", ".cache") / APP
    return _private(path, path)


def config_dir() -> Path:
    path = _xdg("XDG_CONFIG_HOME", ".config") / APP
    return _private(path, path)
