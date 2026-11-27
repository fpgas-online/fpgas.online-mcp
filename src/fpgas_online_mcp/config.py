"""The user's settings: `config.toml` in the broker's config directory
(`$XDG_CONFIG_HOME/fpgas-online/`, normally `~/.config/fpgas-online/config.toml`).

    # How long since the last activity a board's host still counts as in use: a key press in
    # tmux, an ssh terminal in use, a change in ~/Uploads (one value for all of them).
    last_activity = "5m"        # or "300s", "1h", or a number of seconds: 300

A missing file or key means the default. The file is read again on every in-use check, so an edit
applies to the next check without restarting the broker. A value that cannot be read is an error,
never a silent default: the in-use check then says unknown (never idle) and names the file.
"""

import re
import tomllib
from pathlib import Path

from . import paths

DEFAULT_LAST_ACTIVITY_S = 300
MIN_LAST_ACTIVITY_S = 30
MAX_LAST_ACTIVITY_S = 86400
_DURATION = re.compile(r"(\d+)\s*(s|m|h)?")
_UNIT_S = {None: 1, "s": 1, "m": 60, "h": 3600}
KEYS = {"last_activity"}


class ConfigError(ValueError):
    """config.toml exists but a setting in it cannot be used. The message names the file."""


def path() -> Path:
    return paths.config_dir() / "config.toml"


def parse_duration(value: object) -> int:
    """Seconds from a whole number of seconds or a string like "5m", "300s", "1h"."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a duration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (m := _DURATION.fullmatch(value.strip())):
        return int(m[1]) * _UNIT_S[m[2]]
    raise ValueError(f'{value!r} is not a duration (use a number of seconds, or "5m", "300s", "1h")')


def last_activity_s(file: Path | None = None) -> int:
    """The last-activity window in seconds: config.toml's `last_activity`, else the default."""
    try:
        file = path() if file is None else file  # creating the config dir can fail too
        data = tomllib.loads(file.read_text())
    except FileNotFoundError:
        return DEFAULT_LAST_ACTIVITY_S
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{file or 'the config directory'}: cannot be read: {e}") from e
    if unknown := sorted(set(data) - KEYS):
        # the file has one setting: a typo or a table must not leave the user believing it is in force
        raise ConfigError(f"{file}: unknown setting(s) {', '.join(unknown)}; the only one is last_activity")
    if "last_activity" not in data:
        return DEFAULT_LAST_ACTIVITY_S
    try:
        seconds = parse_duration(data["last_activity"])
    except ValueError as e:
        raise ConfigError(f"{file}: last_activity: {e}") from e
    if not MIN_LAST_ACTIVITY_S <= seconds <= MAX_LAST_ACTIVITY_S:
        raise ConfigError(
            f"{file}: last_activity is {seconds} s; it must be from {MIN_LAST_ACTIVITY_S} s to {MAX_LAST_ACTIVITY_S} s"
        )
    return seconds
