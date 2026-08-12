"""Audio player for YouTube Music.

Streams audio through mpv. Stream URLs and autoplay queues are resolved with
yt-dlp (stream extraction) and ytmusicapi (watch playlist) respectively, both
optionally authenticated with YouTube account cookies.
"""

from __future__ import annotations

import locale
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from typing import Any

import requests
import yt_dlp
from mpv import MPV
from yt_dlp.cookies import SUPPORTED_BROWSERS
from ytmusicapi import YTMusic

DEFAULT_PLAYER_CLIENT = "web_embedded"
AUDIO_FORMAT = "bestaudio/best"
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


def _ensure_c_numeric_locale() -> None:
    """ytmusicapi sets LC_ALL to the UI language, but libmpv refuses to
    initialize (hangs) unless LC_NUMERIC is "C"."""
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
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
            artists=_parse_artists(info),
            duration=info.get("duration"),
            ext=info.get("ext") or "",
            format_id=info.get("format_id") or "",
            thumbnail=info.get("thumbnail") or "",
            webpage_url=info.get("webpage_url") or url,
            http_headers=dict(info.get("http_headers") or {}),
        )


def _parse_artists(info: dict[str, Any]) -> list[str]:
    artists = info.get("artists")
    if isinstance(artists, list):
        return [a for a in artists if isinstance(a, str)]
    creator = info.get("creator") or info.get("uploader")
    return [creator] if creator else []


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
        _ensure_c_numeric_locale()
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


class MpvPlayer:
    """Plays audio streams through mpv (python-mpv/libmpv)."""

    def __init__(
        self,
        *,
        volume: int = 80,
        audio_output: str | None = None,
        keep_open: bool = False,
        on_track_end: Callable[[], None] | None = None,
        mpv_factory: Callable[..., MPV] = MPV,
    ) -> None:
        kwargs: dict[str, Any] = {
            "ytdl": False,
            "volume": volume,
            "keep_open": keep_open,
            "idle": True,
        }
        if audio_output:
            kwargs["ao"] = audio_output
        _ensure_c_numeric_locale()
        self._mpv = mpv_factory(**kwargs)
        self._on_track_end = on_track_end
        self._ended = threading.Event()
        self._mpv.observe_property("eof-reached", self._on_eof)

    def _on_eof(self, name: str, value: Any) -> None:
        if value:
            self._ended.set()
            if self._on_track_end:
                self._on_track_end()

    def play(self, stream: StreamInfo) -> None:
        self._ended.clear()
        self._mpv.force_media_title = stream.title
        if stream.http_headers:
            self._mpv.http_header_fields = [
                f"{name}: {value}" for name, value in stream.http_headers.items()
            ]
        self._mpv.play(stream.stream_url)

    def stop(self) -> None:
        self._mpv.stop()

    def pause(self) -> None:
        self._mpv.pause = True

    def resume(self) -> None:
        self._mpv.pause = False

    def toggle(self) -> None:
        self._mpv.pause = not self._mpv.pause

    def seek(self, seconds: float) -> None:
        self._mpv.seek(seconds, "absolute")

    def seek_relative(self, delta: float) -> None:
        self._mpv.seek(delta, "relative")

    @property
    def volume(self) -> int:
        return int(self._mpv.volume)

    @volume.setter
    def volume(self, value: int) -> None:
        self._mpv.volume = max(0, min(100, value))

    @property
    def muted(self) -> bool:
        return bool(self._mpv.mute)

    @muted.setter
    def muted(self, value: bool) -> None:
        self._mpv.mute = value

    @property
    def paused(self) -> bool:
        return bool(self._mpv.pause)

    @property
    def position(self) -> float:
        return float(self._mpv.playback_time or 0.0)

    @property
    def duration(self) -> float | None:
        value = self._mpv.duration
        return float(value) if value else None

    @property
    def media_title(self) -> str:
        return str(self._mpv.media_title or "")

    @property
    def audio_codec(self) -> str:
        return str(self._mpv.audio_codec or "")

    @property
    def eof_reached(self) -> bool:
        return self._ended.is_set()

    @property
    def playing(self) -> bool:
        """Whether a file is loaded and mpv is not sitting idle."""
        return not bool(self._mpv.idle_active) and bool(self._mpv.playlist)

    def wait_for_end(self, timeout: float | None = None) -> bool:
        return self._ended.wait(timeout)

    def close(self) -> None:
        self._mpv.terminate()
