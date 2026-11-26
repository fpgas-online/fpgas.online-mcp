"""XDG directories used by the broker, front end and CLI. Each is created mode 0700."""

import os
from pathlib import Path

APP = "fpgas-online"
MAX_SOCKET_PATH = 107  # bytes: sun_path is 108 with the trailing NUL
BROKER_SOCK = "broker.sock"
# ssh's ControlPath "cm-%C" is "cm-" + 40 hex chars, and ssh adds a temporary suffix of about 20
# while it creates the master socket.
CONTROL_PATH_ROOM = len("/cm-") + 40 + 20


class PathTooLong(ValueError):
    """A Unix socket under the runtime directory would be longer than the kernel allows."""


def _too_long(path: str) -> PathTooLong:
    return PathTooLong(
        f"the socket path {path} would be {len(path.encode())} bytes, more than the {MAX_SOCKET_PATH} Unix sockets "
        "allow; set XDG_RUNTIME_DIR to a shorter directory (for example /run/user/<uid>)"
    )


def check_control_path_room(run: Path) -> None:
    """Raise PathTooLong if ssh's master sockets (ControlPath cm-%C) would not fit under `run`."""
    if len(str(run).encode()) + CONTROL_PATH_ROOM > MAX_SOCKET_PATH:
        raise _too_long(f"{run}/cm-<40 hex chars>.<ssh's temporary suffix>")


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
    """The runtime directory; PathTooLong if the broker socket in it would be too long for a Unix socket."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        top = Path(runtime) / APP
        run, top_of = top, top
    else:
        top_of = _xdg("XDG_CACHE_HOME", ".cache") / APP
        run = top_of / "run"
    sock = f"{run}/{BROKER_SOCK}"
    if len(sock.encode()) > MAX_SOCKET_PATH:
        raise _too_long(sock)
    return _private(run, top_of)


def state_dir() -> Path:
    path = _xdg("XDG_STATE_HOME", ".local/state") / APP
    return _private(path, path)


def cache_dir() -> Path:
    path = _xdg("XDG_CACHE_HOME", ".cache") / APP
    return _private(path, path)


def config_dir() -> Path:
    path = _xdg("XDG_CONFIG_HOME", ".config") / APP
    return _private(path, path)
