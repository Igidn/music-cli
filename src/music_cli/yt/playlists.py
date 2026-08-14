from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ytmusicapi import YTMusic

from ..core.errors import PlayerError
from .cookies import Cookies, _account_session, _browser_auth
from .extract import PlaylistTrack, parse_watch_track


@dataclass(frozen=True)
class LibraryPlaylist:
    """One playlist from the signed-in account's library."""

    playlist_id: str
    title: str
    track_count: str = ""


def parse_library_playlist(raw: dict[str, Any]) -> LibraryPlaylist:
    count = raw.get("count") or raw.get("itemCount") or ""
    return LibraryPlaylist(
        playlist_id=raw.get("playlistId") or "",
        title=raw.get("title") or "Untitled playlist",
        track_count=str(count),
    )


class Library:
    """The account's library playlists and their tracks.

    Authenticates with browser cookies captured by ``music-cli login``.
    """

    def __init__(
        self,
        api: YTMusic | None = None,
        cookies: Cookies | None = None,
        *,
        limit: int = 25,
    ) -> None:
        self._api = api
        self._cookies = cookies
        self._limit = limit

    @property
    def authenticated(self) -> bool:
        """Whether account credentials are configured for library access."""
        return bool(self._cookies and self._cookies.enabled)

    def _client(self) -> YTMusic:
        if self._api is not None:
            return self._api
        if self._cookies and self._cookies.enabled:
            session = _account_session(self._cookies)
            return YTMusic(auth=_browser_auth(session), requests_session=session)
        return YTMusic()

    def playlists(self, limit: int | None = None) -> list[LibraryPlaylist]:
        """The library playlists, in the account's own order."""
        data = self._client().get_library_playlists(
            limit=limit if limit is not None else self._limit
        )
        return [
            playlist
            for playlist in (parse_library_playlist(item) for item in data)
            if playlist.playlist_id
        ]

    def tracks(self, playlist_id: str) -> list[PlaylistTrack]:
        """Every playable track in ``playlist_id``.

        Tracks the API marks unavailable (no video id) are skipped.
        """
        data = self._client().get_playlist(playlistId=playlist_id)
        tracks = [parse_watch_track(item) for item in data.get("tracks", [])]
        return [track for track in tracks if track.video_id]

    def create_playlist(self, title: str) -> str:
        """Create a new private playlist and return its id."""
        result = self._client().create_playlist(title, "", privacy_status="PRIVATE")
        return (
            result if isinstance(result, str) else str(result.get("playlistId") or "")
        )

    def rename_playlist(self, playlist_id: str, title: str) -> None:
        """Rename ``playlist_id``; other properties are left untouched."""
        self._client().edit_playlist(playlist_id, title=title)

    def add_tracks(self, playlist_id: str, video_ids: list[str]) -> None:
        """Append ``video_ids`` to ``playlist_id``."""
        self._client().add_playlist_items(playlist_id, video_ids)

    def remove_track(self, playlist_id: str, video_id: str) -> None:
        """Remove every occurrence of ``video_id`` from ``playlist_id``.

        The API needs each occurrence's ``setVideoId``, so the playlist is
        fetched first and only matching items are removed.
        """
        data = self._client().get_playlist(playlistId=playlist_id)
        videos = [
            {"videoId": item.get("videoId"), "setVideoId": item.get("setVideoId")}
            for item in data.get("tracks", [])
            if item.get("videoId") == video_id
        ]
        if not videos:
            raise PlayerError(f"Track {video_id} is not in this playlist")
        self._client().remove_playlist_items(playlist_id, videos)
