"""Shared test setup."""

import shutil
import tempfile
from pathlib import Path

import pytest
from tmpdir import TEST_TMP


@pytest.fixture(autouse=True)
def own_config_dir(monkeypatch):
    """Every test gets its own empty config directory: none ever reads the user's config.toml."""
    TEST_TMP.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=TEST_TMP, prefix="cfg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)
