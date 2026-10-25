"""Shared test setup.

The desk's settings loader reads the `.env` file next to the code and the process environment. On the owner's server
both hold the real configuration, and the update job (which loads `.env` into its own environment) runs this suite
before every install, so no test may depend on either: every test sees no settings file and none of the settings
variables, unless it sets its own.
"""
import pytest

from trader import config


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "no-such.env")
    for key in config.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
