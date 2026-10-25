"""Shared test setup.

The desk's settings loader reads the `.env` file next to the code. On the owner's server that file holds the
real configuration, and the updater runs this suite there before every install, so no test may depend on it:
every test sees an empty settings file unless it writes its own.
"""
import pytest

from trader import config


@pytest.fixture(autouse=True)
def isolated_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "no-such.env")
