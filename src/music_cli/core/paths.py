"""Filesystem locations for the app's config and cache state."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAME = "music-cli"


def config_dir() -> Path:
    """The music-cli config directory (``$MUSIC_CLI_CONFIG_DIR`` or XDG)."""
    override = os.environ.get("MUSIC_CLI_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / CONFIG_DIR_NAME
