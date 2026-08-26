"""Shared types and utilities used by all player backends.

``LocalFile``, ``_download_temp``, and ``_default_fetch_stream`` are
cross-platform and live here so both the AVFoundation (macOS) and GStreamer
(Linux/other) backends can use them without importing each other's
platform-specific dependencies.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import PlayerError
from ..storage.cache import AudioCache, DownloadResult, TrackMeta
from ..yt.cookies import Cookies
from ..yt.extract import StreamExtractor, StreamInfo


@dataclass(frozen=True)
class LocalFile:
    """A local audio file handed to the player by a stream fetcher.

    ``owned`` files are temporary downloads the player deletes when they are
    replaced or playback stops; cache-managed files (``owned=False``) are
    removed by the cache's eviction policy instead.
    """

    path: str
    owned: bool = True


def download_temp(
    extractor: StreamExtractor,
    stream: StreamInfo,
    *,
    progress_hook: Callable | None = None,
) -> LocalFile:
    """Download ``stream`` to a temporary file the player owns and deletes."""
    file = tempfile.NamedTemporaryFile(prefix="music-cli-", delete=False)
    file.close()
    try:
        return LocalFile(
            extractor.download(stream.video_id, file.name, progress_hook=progress_hook),
            owned=True,
        )
    except PlayerError:
        try:
            os.unlink(file.name)
        except OSError:
            pass
        raise


def default_fetch_stream(
    cookies: Cookies | None,
    cache: AudioCache | None = None,
    *,
    extractor: StreamExtractor | None = None,
    download_progress: Callable[[], Callable[[dict[str, Any]], None] | None]
    | None = None,
) -> Callable[[StreamInfo], LocalFile]:
    """Build the default stream fetcher: cache-aware, else a temp download.

    Tracks already in the cache are returned directly (no network at all);
    the rest are downloaded with yt-dlp into the cache, with concurrent
    requests for the same video sharing one download. Without a cache,
    downloads go to temporary files the player deletes after playback.
    """
    extractor = extractor or StreamExtractor(cookies)
    get_hook = download_progress or (lambda: None)

    def fetch(stream: StreamInfo) -> LocalFile:
        if cache is None:
            return download_temp(extractor, stream, progress_hook=get_hook())

        def downloader(target: Path) -> DownloadResult:
            filepath = extractor.download(
                stream.video_id, str(target), progress_hook=get_hook()
            )
            return DownloadResult(
                path=filepath,
                meta=TrackMeta(
                    title=stream.title,
                    artists=tuple(stream.artists),
                    duration=stream.duration,
                    ext=Path(filepath).suffix.lstrip("."),
                ),
            )

        path = cache.get_or_download(stream.video_id, downloader)
        if path is None:
            raise PlayerError(f"Failed to download {stream.video_id}")
        return LocalFile(str(path), owned=False)

    return fetch
