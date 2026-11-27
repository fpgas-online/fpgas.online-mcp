"""config.toml: the one last-activity window, read on every in-use check."""

import re

import pytest

from fpgas_online_mcp import config, inuse
from fpgas_online_mcp.boards import Board, BoardId


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ('last_activity = "5m"\n', 300),
        ('last_activity = "300s"\n', 300),
        ('last_activity = "1h"\n', 3600),
        ('last_activity = "90"\n', 90),
        ("last_activity = 120\n", 120),
        ('last_activity = " 10m "\n', 600),
        ("", config.DEFAULT_LAST_ACTIVITY_S),
    ],
)
def test_last_activity_values(own_config_dir, text, seconds):
    f = own_config_dir / "config.toml"
    f.write_text(text)
    assert config.last_activity_s(f) == seconds


def test_no_file_is_the_default(own_config_dir):
    assert config.last_activity_s(own_config_dir / "config.toml") == 300 == config.DEFAULT_LAST_ACTIVITY_S


@pytest.mark.parametrize(
    "text",
    [
        'last_activity = "5 minutes"\n',
        'last_activity = "-5m"\n',
        "last_activity = true\n",
        "last_activity = 1.5\n",
        "last_activity = 10\n",  # under 30 s
        'last_activity = "2d"\n',
        "last_activity = 86401\n",
        "last_activity = = \n",  # not TOML
    ],
)
def test_a_bad_value_is_an_error_naming_the_file(own_config_dir, text):
    f = own_config_dir / "config.toml"
    f.write_text(text)
    with pytest.raises(config.ConfigError, match=str(f)):
        config.last_activity_s(f)


def test_the_script_has_one_window_and_takes_the_value():
    s = inuse.remote_script(600)
    assert s.count(" win=600;") == 1 and "win=300" not in s
    assert len(re.findall(r"\bwin=\d", s)) == 1  # the only number the script's window is set to
    assert "tmux_win" not in s


def test_the_script_reads_config_toml(own_config_dir):
    (own_config_dir / "fpgas-online").mkdir(mode=0o700)
    (own_config_dir / "fpgas-online" / "config.toml").write_text('last_activity = "2m"\n')
    assert "win=120;" in inuse.remote_script()
    (own_config_dir / "fpgas-online" / "config.toml").write_text('last_activity = "7m"\n')  # read again, no restart
    assert "win=420;" in inuse.remote_script()


def test_every_window_in_the_script_is_win():
    """tmux clients, tmux windows, ssh terminals, ~/Uploads and sftp-server all use the one `win`."""
    s = inuse.remote_script(300)
    for needle in ('-v win="$win"', '"$tmin" -lt "$win"', '[ "$v" -lt "$win" ]', "et[q] >= win"):
        assert needle in s, needle
    assert s.count('-v win="$win"') == 2


async def test_a_bad_config_makes_the_check_unknown(own_config_dir):
    (own_config_dir / "fpgas-online").mkdir(mode=0o700)
    (own_config_dir / "fpgas-online" / "config.toml").write_text('last_activity = "soon"\n')
    b = Board(BoardId("welland", "pi-sw1-p2"), "welland", "pi-sw1-p2", None, "operational", True, None, None, None,
              "2026-11-27T12:00:00Z", False)  # fmt: skip
    v = await inuse.check(b, _NeverSsh(), None, None)
    assert v.verdict == "unknown" and "ConfigError" in v.signals[0] and "config.toml" in v.signals[0]


class _NeverSsh:
    async def run(self, *a, **k):
        raise AssertionError("ssh must not run with a bad config")


@pytest.mark.parametrize("text", ['last_activty = "15m"\n', '[broker]\nlast_activity = "15m"\n'])
def test_an_unknown_setting_is_an_error(own_config_dir, text):
    """A typo or a table must not leave the user believing their value is in force."""
    f = own_config_dir / "config.toml"
    f.write_text(text)
    with pytest.raises(config.ConfigError, match="unknown setting"):
        config.last_activity_s(f)


def test_a_config_dir_that_cannot_be_made_is_a_config_error(monkeypatch):
    def broken():
        raise PermissionError("read-only file system")

    monkeypatch.setattr(config, "path", broken)
    with pytest.raises(config.ConfigError, match="read-only"):
        config.last_activity_s()
