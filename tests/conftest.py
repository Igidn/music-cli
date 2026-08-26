"""Shared fixtures: keep every test away from the developer's real config dir.

The TUI persists the last played track under the config directory
(``MUSIC_CLI_CONFIG_DIR``), so tests must never read or write the real one.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path / "config"))
