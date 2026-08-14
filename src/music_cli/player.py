"""Audio player for YouTube Music.

Streams audio through AVFoundation (``AVPlayer`` + ``AVURLAsset``) via pyobjc.
Stream URLs and autoplay queues are resolved with yt-dlp (stream extraction)
and ytmusicapi (watch playlist) respectively, both optionally authenticated
with YouTube account cookies.

googlevideo rejects plain HTTP fetches of these stream URLs (they require
decoded signing parameters and byte-range requests), so each track is
downloaded to a temporary file with yt-dlp before AVFoundation plays it.
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from AVFoundation import (
    AVPlayer,
    AVPlayerActionAtItemEndPause,
    AVPlayerItem,
    AVPlayerItemDidPlayToEndTimeNotification,
    AVPlayerItemStatusFailed,
    AVMutableAudioMix,
    AVMutableAudioMixInputParameters,
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
from yt_dlp.cookies import SUPPORTED_BROWSERS
from ytmusicapi import YTMusic

from .cache import AudioCache, DownloadResult, TrackMeta
from .search import parse_artists



# Not exposed by pyobjc's AVFoundation bindings; the constant's runtime value
# is its own name string (see AVURLAsset.h).
AVURLAssetHTTPHeaderFieldsKey = "AVURLAssetHTTPHeaderFieldsKey"

DEFAULT_PLAYER_CLIENT = "web_embedded"
# How long play() keeps re-issuing the play request when the end-of-item
# pause of the previous track drops it (see _ensure_playback_started).
PLAY_RATE_TIMEOUT = 1.0
# Prefer AAC in an MP4 container: AVFoundation plays it on every system,
# whereas WebM/Opus support is missing on some macOS installs (the system's
# own avconvert fails on webm, so downloads falling back to opus simply
# won't play). bestaudio remains as a last resort.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PlayerError(Exception):
    """Raised when a stream cannot be resolved or played."""


class _QuietLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


@dataclass(frozen=True)
class Cookies:
    """YouTube account cookies.

    Either a manual Netscape-format cookie file (``cookiefile``) or yt-dlp's
    browser cookie extractor (``cookiesfrombrowser``). For now the manual
    file is the supported path for account-aware playback.
    """

    cookiefile: str | None = None
    cookiesfrombrowser: tuple[str, ...] | None = None

    @classmethod
    def from_file(cls, path: str) -> Cookies:
        return cls(cookiefile=path)

    @classmethod
    def from_browser(
        cls,
        browser: str,
        profile: str | None = None,
        keyring: str | None = None,
        container: str | None = None,
    ) -> Cookies:
        if browser not in SUPPORTED_BROWSERS:
            supported = ", ".join(sorted(SUPPORTED_BROWSERS))
            raise PlayerError(
                f"Unsupported browser {browser!r} for cookie extraction. "
                f"Supported: {supported}"
            )
        return cls(
            cookiesfrombrowser=tuple(
                x for x in (browser, profile, keyring, container) if x
            )
        )

    @property
    def enabled(self) -> bool:
        return bool(self.cookiefile or self.cookiesfrombrowser)

    def ydl_options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        if self.cookiefile:
            opts["cookiefile"] = self.cookiefile
        if self.cookiesfrombrowser:
            opts["cookiesfrombrowser"] = self.cookiesfrombrowser
        return opts

    def requests_session(self) -> requests.Session | None:
        """A requests session carrying these cookies, for ytmusicapi.

        Only the manual cookie file is supported (ytmusicapi has no browser
        cookie extractor); without a file an anonymous session is used.
        """
        if not self.cookiefile:
            return None
        if not os.path.isfile(self.cookiefile):
            raise PlayerError(f"Cookie file not found: {self.cookiefile}")
        jar = MozillaCookieJar(self.cookiefile)
        jar.load(ignore_discard=True, ignore_expires=True)
        session = requests.Session()
        session.cookies = jar
        session.headers.update({"User-Agent": USER_AGENT})
        return session


@dataclass(frozen=True)
class StreamInfo:
    """A resolvable audio stream for one video."""

    video_id: str
    title: str
    stream_url: str
    artists: list[str] = field(default_factory=list)
    duration: float | None = None
    ext: str = ""
    format_id: str = ""
    thumbnail: str = ""
    webpage_url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalFile:
    """A local audio file handed to the player by a stream fetcher.

    ``owned`` files are temporary downloads the player deletes when they are
    replaced or playback stops; cache-managed files (``owned=False``) are
    removed by the cache's eviction policy instead.
    """

    path: str
    owned: bool = True


@dataclass(frozen=True)
class PlaylistTrack:
    """A track from the YouTube Music watch playlist (autoplay queue)."""

    video_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    duration: str = ""
    video_type: str = ""
    thumbnail: str = ""
    counterpart_video_id: str = ""


class StreamExtractor:
    """Resolves playable audio stream URLs with yt-dlp."""

    def __init__(
        self,
        cookies: Cookies | None = None,
        *,
        player_client: str = DEFAULT_PLAYER_CLIENT,
        format_selector: str = AUDIO_FORMAT,
        ydl_factory: Callable[[dict[str, Any]], yt_dlp.YoutubeDL] = yt_dlp.YoutubeDL,
    ) -> None:
        self._cookies = cookies
        self._player_client = player_client
        self._format_selector = format_selector
        self._ydl_factory = ydl_factory

    def _options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "format": self._format_selector,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 30,
            "extractor_retries": 3,
            "extractor_args": {
                "youtube": {"player_client": [self._player_client]},
            },
            "logger": _QuietLogger(),
        }
        if self._cookies and self._cookies.enabled:
            opts.update(self._cookies.ydl_options())
        return opts

    def resolve(self, video_id: str) -> StreamInfo:
        if not video_id:
            raise PlayerError("A video id is required to resolve a stream")
        url = f"https://www.youtube.com/watch?v={video_id}"
        with self._ydl_factory(self._options()) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as error:
                raise PlayerError(
                    f"Failed to extract stream for {video_id}: {error}"
                ) from error
        if not info.get("url"):
            raise PlayerError(f"No playable audio format found for {video_id}")
        return StreamInfo(
            video_id=info.get("id") or video_id,
            title=info.get("title") or video_id,
            stream_url=info["url"],
            artists=parse_artists(info),
            duration=info.get("duration"),
            ext=info.get("ext") or "",
            format_id=info.get("format_id") or "",
            thumbnail=info.get("thumbnail") or "",
            webpage_url=info.get("webpage_url") or url,
            http_headers=dict(info.get("http_headers") or {}),
        )

    def download(self, video_id: str, outtmpl: str) -> str:
        """Download the best audio for ``video_id`` to a file, returning its path.

        Re-extracts so the signing parameters (``n``, ``pot``) are freshly
        decoded: googlevideo frequently rejects direct fetches of previously
        extracted URLs with HTTP 403.
        """
        if not video_id:
            raise PlayerError("A video id is required to download a stream")
        url = f"https://www.youtube.com/watch?v={video_id}"
        opts: dict[str, Any] = {
            **self._options(),
            "skip_download": False,
            "outtmpl": outtmpl + ".%(ext)s",
        }
        with self._ydl_factory(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as error:
                raise PlayerError(
                    f"Failed to download stream for {video_id}: {error}"
                ) from error
        downloads = (info or {}).get("requested_downloads") or []
        if not downloads or not downloads[0].get("filepath"):
            raise PlayerError(f"No audio was downloaded for {video_id}")
        return downloads[0]["filepath"]

class WatchPlaylist:
    """Autoplay queue from the YouTube Music watch playlist endpoint.

    Uses ytmusicapi's ``get_watch_playlist(videoId=...)`` with the account
    cookies so the queue reflects the signed-in account.
    """

    def __init__(
        self,
        api: YTMusic | None = None,
        cookies: Cookies | None = None,
        *,
        limit: int = 25,
        radio: bool = False,
        shuffle: bool = False,
    ) -> None:
        self._api = api
        self._cookies = cookies
        self._limit = limit
        self._radio = radio
        self._shuffle = shuffle

    def _client(self) -> YTMusic:
        if self._api is not None:
            return self._api
        session = self._cookies.requests_session() if self._cookies else None
        client = YTMusic(requests_session=session) if session else YTMusic()
        return client

    def get(
        self,
        video_id: str | None = None,
        playlist_id: str | None = None,
        *,
        limit: int | None = None,
        radio: bool | None = None,
        shuffle: bool | None = None,
    ) -> list[PlaylistTrack]:
        if video_id is None and playlist_id is None:
            raise PlayerError("Either videoId or playlistId is required")
        data = self._client().get_watch_playlist(
            videoId=video_id,
            playlistId=playlist_id,
            limit=limit if limit is not None else self._limit,
            radio=radio if radio is not None else self._radio,
            shuffle=shuffle if shuffle is not None else self._shuffle,
        )
        return [parse_watch_track(track) for track in data.get("tracks", [])]


def parse_watch_track(raw: dict[str, Any]) -> PlaylistTrack:
    artists = [
        artist["name"]
        for artist in (raw.get("artists") or [])
        if isinstance(artist, dict) and artist.get("name")
    ]
    counterpart = raw.get("counterpart")
    thumbnail = raw.get("thumbnail")
    return PlaylistTrack(
        video_id=raw.get("videoId") or "",
        title=raw.get("title") or "",
        artists=artists,
        duration=raw.get("length") or "",
        video_type=raw.get("videoType") or "",
        thumbnail=thumbnail[0]["url"]
        if isinstance(thumbnail, list) and thumbnail
        else "",
        counterpart_video_id=counterpart.get("videoId")
        if isinstance(counterpart, dict)
        else "",
    )


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

    No ``NSApplication`` is ever created, so the process never gets a Dock
    icon by construction; audio output goes straight through CoreAudio.

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
        """Service the main run loop for one short slice.

        AVFoundation advances the media pipeline only while the main run loop
        runs; the TUI calls this from its own event loop on the main thread.
        """
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.02)
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
