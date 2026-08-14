"""Audio playback through AVFoundation (macOS only).

Streams audio through ``AVPlayer`` + ``AVURLAsset`` via pyobjc. googlevideo
rejects plain HTTP fetches of stream URLs, so each track is downloaded to a
local file (cache-managed or temporary) before AVFoundation plays it.

No ``NSApplication`` is ever created, so the process never gets a Dock icon
by construction; audio output goes straight through CoreAudio.
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from AVFoundation import (
    AVMutableAudioMix,
    AVMutableAudioMixInputParameters,
    AVPlayer,
    AVPlayerActionAtItemEndPause,
    AVPlayerItem,
    AVPlayerItemDidPlayToEndTimeNotification,
    AVPlayerItemStatusFailed,
    AVURLAsset,
    CMTimeGetSeconds,
    CMTimeMakeWithSeconds,
    kCMTimeZero,
)
from Foundation import (
    NSURL,
    NSDate,
    NSDefaultRunLoopMode,
    NSNotificationCenter,
    NSRunLoop,
)

from ..core.errors import PlayerError
from ..storage.cache import AudioCache, DownloadResult, TrackMeta
from ..yt.cookies import Cookies
from ..yt.extract import StreamExtractor, StreamInfo

# Not exposed by pyobjc's AVFoundation bindings; the constant's runtime value
# is its own name string (see AVURLAsset.h).
AVURLAssetHTTPHeaderFieldsKey = "AVURLAssetHTTPHeaderFieldsKey"
# How long play() keeps re-issuing the play request when the end-of-item
# pause of the previous track drops it (see _ensure_playback_started).
PLAY_RATE_TIMEOUT = 1.0


@dataclass(frozen=True)
class LocalFile:
    """A local audio file handed to the player by a stream fetcher.

    ``owned`` files are temporary downloads the player deletes when they are
    replaced or playback stops; cache-managed files (``owned=False``) are
    removed by the cache's eviction policy instead.
    """

    path: str
    owned: bool = True


def _cmtime_seconds(value: Any) -> float | None:
    """Seconds from a CMTime, or ``None`` for non-numeric values.

    Indefinite (streaming until the container is parsed) and infinite times
    yield ``None`` so callers can fall back to a sensible default.
    """
    seconds = CMTimeGetSeconds(value)
    if math.isnan(seconds) or math.isinf(seconds):
        return None
    return seconds


def _default_player() -> AVPlayer:
    return AVPlayer.alloc().init()


def _download_temp(extractor: StreamExtractor, stream: StreamInfo) -> LocalFile:
    """Download ``stream`` to a temporary file the player owns and deletes."""
    file = tempfile.NamedTemporaryFile(prefix="music-cli-", delete=False)
    file.close()
    try:
        return LocalFile(extractor.download(stream.video_id, file.name), owned=True)
    except PlayerError:
        try:
            os.unlink(file.name)
        except OSError:
            pass
        raise


def _default_fetch_stream(
    cookies: Cookies | None,
    cache: AudioCache | None = None,
    *,
    extractor: StreamExtractor | None = None,
) -> Callable[[StreamInfo], LocalFile]:
    """Build the default stream fetcher: cache-aware, else a temp download.

    Tracks already in the cache are returned directly (no network at all);
    the rest are downloaded with yt-dlp into the cache, with concurrent
    requests for the same video sharing one download. Without a cache,
    downloads go to temporary files the player deletes after playback.
    """
    extractor = extractor or StreamExtractor(cookies)

    def fetch(stream: StreamInfo) -> LocalFile:
        if cache is None:
            return _download_temp(extractor, stream)

        def downloader(target: Path) -> DownloadResult:
            filepath = extractor.download(stream.video_id, str(target))
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


class AVFoundationPlayer:
    """Plays audio streams through AVFoundation (AVPlayer/AVURLAsset).

    AVFoundation's media pipeline is serviced by the main ``NSRunLoop``, so
    the main thread must pump it periodically via :meth:`pump`. The TUI does
    this automatically while the app is running. All AVPlayer access is
    thread-safe; only :meth:`pump` must run on the main thread.
    """

    def __init__(
        self,
        *,
        volume: int = 80,
        loop: bool = False,
        on_track_end: Callable[[], None] | None = None,
        cookies: Cookies | None = None,
        player_factory: Callable[[], AVPlayer] = _default_player,
        fetch_stream: Callable[[StreamInfo], LocalFile] | None = None,
        cache: AudioCache | None = None,
    ) -> None:
        self.on_track_end = on_track_end
        self._loop = loop
        self._ended = threading.Event()
        self._title = ""
        self._observer_token: Any = None
        self._current_item: AVPlayerItem | None = None
        self._local_url: str | None = None
        self._fetch_stream = fetch_stream or _default_fetch_stream(cookies, cache=cache)
        self._player = player_factory()
        # Volume lives on the track's audio mix, not the player: AVPlayer.volume
        # is unreliable for macOS local-file streams, so it is kept at unity and
        # the real level is carried per item (see _apply_volume).
        self._volume = max(0, min(100, int(volume)))
        self._player.setVolume_(1.0)
        self._player.setMuted_(False)
        self._player.setActionAtItemEnd_(AVPlayerActionAtItemEndPause)

    def pump(self) -> None:
        """Service the main run loop once, without blocking.

        AVFoundation advances the media pipeline only while the main run loop
        runs; the TUI calls this from its own event loop on the main thread.
        The deadline is "now" so pending sources are drained but the call
        returns immediately: a blocking wait here would freeze TUI input and
        rendering for its whole duration, several times per second.
        """
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.date()
        )

    def play(self, stream: StreamInfo) -> None:
        """Fetch the stream locally and start playing it.

        Streams that cannot be fetched leave the player idle; the client's
        retry loop detects that and tries a fresh URL.
        """
        self._ended.clear()
        self._unobserve()
        self._remove_local_file()
        try:
            local = self._fetch_stream(stream)
        except PlayerError:
            self._title = ""
            self._current_item = None
            self._player.replaceCurrentItemWithPlayerItem_(None)
            return
        self._title = stream.title
        url = local.path if local.path.startswith("file://") else f"file://{local.path}"
        if local.owned:
            self._local_url = url
        asset = AVURLAsset.alloc().initWithURL_options_(
            NSURL.URLWithString_(url),
            {AVURLAssetHTTPHeaderFieldsKey: stream.http_headers},
        )
        item = AVPlayerItem.alloc().initWithAsset_(asset)
        self._observer_token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AVPlayerItemDidPlayToEndTimeNotification,
            item,
            None,
            self._on_item_ended,
        )
        self._current_item = item
        self._player.replaceCurrentItemWithPlayerItem_(item)
        self._apply_volume()
        self._ensure_playback_started()

    def _apply_volume(self) -> None:
        """Push the current volume onto the active item's audio mix.

        ``AVPlayer.volume`` is not honoured reliably on macOS for these
        local-file streams, so each new item gets its own mix carrying the
        level, keeping the player itself at unity.
        """
        if self._current_item is None:
            return
        params = AVMutableAudioMixInputParameters.audioMixInputParametersWithTrack_(
            None
        )
        params.setVolume_atTime_(self._volume / 100.0, kCMTimeZero)
        mix = AVMutableAudioMix.alloc().init()
        mix.setInputParameters_([params])
        self._current_item.setAudioMix_(mix)

    def _ensure_playback_started(self) -> None:
        """Start playback, re-issuing play() until the rate actually rises.

        The player pauses when an item ends (actionAtItemEnd is Pause), and
        that end-of-item pause can land *after* a play request issued for the
        next track — leaving the new item loaded but silent. This is exactly
        the auto-next path: the EOF notification fires, then the next track
        is resolved and played in quick succession. Keep re-requesting
        playback for a short window so the next track reliably starts.
        """
        deadline = time.monotonic() + PLAY_RATE_TIMEOUT
        while True:
            self._player.play()
            self.pump()
            time.sleep(0.05)
            if self._player.rate() != 0.0:
                time.sleep(0.1)
                if self._player.rate() != 0.0:
                    return
            if time.monotonic() >= deadline:
                return

    def stop(self) -> None:
        self._unobserve()
        self._current_item = None
        self._player.replaceCurrentItemWithPlayerItem_(None)
        self._remove_local_file()

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.play()

    def toggle(self) -> None:
        if self.paused:
            self.resume()
        else:
            self.pause()

    def seek(self, seconds: float) -> None:
        if self._current_item is None:
            return
        self._player.seekToTime_toleranceBefore_toleranceAfter_(
            CMTimeMakeWithSeconds(seconds, 600), kCMTimeZero, kCMTimeZero
        )

    def seek_relative(self, delta: float) -> None:
        self.seek(self.position + delta)

    def _on_item_ended(self, notification: Any) -> None:
        if self._loop:
            self._player.seekToTime_toleranceBefore_toleranceAfter_(
                kCMTimeZero, kCMTimeZero, kCMTimeZero
            )
            self._player.play()
            return
        self._ended.set()
        if self.on_track_end:
            self.on_track_end()

    def _unobserve(self) -> None:
        if self._observer_token is not None:
            NSNotificationCenter.defaultCenter().removeObserver_(self._observer_token)
            self._observer_token = None

    def _remove_local_file(self) -> None:
        if self._local_url is not None:
            try:
                os.unlink(self._local_url.removeprefix("file://"))
            except OSError:
                pass
            self._local_url = None

    @property
    def volume(self) -> int:
        return self._volume

    @volume.setter
    def volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._apply_volume()

    @property
    def muted(self) -> bool:
        return bool(self._player.isMuted())

    @muted.setter
    def muted(self, value: bool) -> None:
        self._player.setMuted_(bool(value))

    @property
    def loop(self) -> bool:
        return self._loop

    @loop.setter
    def loop(self, value: bool) -> None:
        self._loop = bool(value)

    @property
    def paused(self) -> bool:
        return self._player.rate() == 0.0

    @property
    def position(self) -> float:
        return _cmtime_seconds(self._player.currentTime()) or 0.0

    @property
    def duration(self) -> float | None:
        if self._current_item is None:
            return None
        return _cmtime_seconds(self._current_item.duration())

    @property
    def media_title(self) -> str:
        return self._title

    @property
    def eof_reached(self) -> bool:
        return self._ended.is_set()

    @property
    def playing(self) -> bool:
        """Whether a track is loaded and its item hasn't failed to load."""
        if self._current_item is None:
            return False
        return int(self._current_item.status()) != AVPlayerItemStatusFailed

    def wait_for_end(self, timeout: float | None = None) -> bool:
        return self._ended.wait(timeout)

    def close(self) -> None:
        self.stop()
