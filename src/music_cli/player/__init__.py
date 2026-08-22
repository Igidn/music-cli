"""Audio playback backends.

Auto-selects the platform-appropriate player:

* **macOS**  — ``AVFoundationPlayer`` (via pyobjc)
* **Linux** (and other platforms) — ``GStreamerPlayer`` (via GStreamer/PyGObject)

Both expose the same public interface so ``MusicClient`` and the rest of the
codebase are platform-agnostic.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from .base import LocalFile, default_fetch_stream, download_temp

__all__ = [
    "LocalFile",
    "default_fetch_stream",
    "download_temp",
    "create_player",
]


def create_player(
    *,
    volume: int = 80,
    loop: bool = False,
    on_track_end: Callable[[], None] | None = None,
    cookies: "Cookies | None" = None,
    cache: "AudioCache | None" = None,
    fetch_stream: Callable[["StreamInfo"], LocalFile] | None = None,
    download_progress: Callable[
        [], Callable[[dict[str, Any]], None] | None
    ] | None = None,
) -> object:
    """Create a platform-appropriate audio player instance.

    All keyword arguments are forwarded to the player backend's constructor:

    * ``volume`` — initial volume (0-100, default 80)
    * ``loop`` — whether to loop the current track (default False)
    * ``on_track_end`` — callback invoked when a track finishes
    * ``cookies`` — :class:`~music_cli.yt.cookies.Cookies` for stream auth
    * ``cache`` — :class:`~music_cli.storage.cache.AudioCache`
    * ``fetch_stream`` — callable that resolves a :class:`~.yt.extract.StreamInfo`
      to a local file (:class:`~.player.base.LocalFile`). Defaults to
      :func:`base.default_fetch_stream`.
    * ``download_progress`` — download progress hook factory

    On macOS the returned player is a :class:`~.audio.AVFoundationPlayer`;
    on all other platforms it is a :class:`~.gst.GStreamerPlayer`.
    """
    if sys.platform == "darwin":
        from .audio import AVFoundationPlayer as Player  # type: ignore[import-untyped]
    else:
        from .gst import GStreamerPlayer as Player  # type: ignore[import-untyped]

    return Player(
        volume=volume,
        loop=loop,
        on_track_end=on_track_end,
        cookies=cookies,
        cache=cache,
        fetch_stream=fetch_stream,
        download_progress=download_progress,
    )