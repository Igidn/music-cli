"""Resolving playable streams and the YouTube Music watch playlist."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yt_dlp
from ytmusicapi import YTMusic

from ..core.errors import PlayerError
from .cookies import Cookies
from .search import parse_artists

DEFAULT_PLAYER_CLIENT = "web_embedded"
# Prefer AAC in an MP4 container: AVFoundation plays it on every system,
# whereas WebM/Opus support is missing on some macOS installs (the system's
# own avconvert fails on webm, so downloads falling back to opus simply
# won't play). bestaudio remains as a last resort.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio"


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


class _QuietLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


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

    def download(
        self, video_id: str, outtmpl: str, *, progress_hook: Callable[[dict[str, Any]], None] | None = None
    ) -> str:
        """Download the best audio for ``video_id`` to a file, returning its path.

        Re-extracts so the signing parameters (``n``, ``pot``) are freshly
        decoded: googlevideo frequently rejects direct fetches of previously
        extracted URLs with HTTP 403.

        ``progress_hook``, when given, is registered as a yt-dlp progress hook,
        called with the ``{'status': 'downloading', ...}`` dict as the download
        progresses.
        """
        if not video_id:
            raise PlayerError("A video id is required to download a stream")
        url = f"https://www.youtube.com/watch?v={video_id}"
        opts: dict[str, Any] = {
            **self._options(),
            "skip_download": False,
            "outtmpl": outtmpl + ".%(ext)s",
        }
        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]
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
