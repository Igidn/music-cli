

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from yt_dlp.cookies import extract_cookies_from_browser
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi import setup_oauth as ytm_setup_oauth

from .player import USER_AGENT, Cookies, PlayerError, PlaylistTrack, parse_watch_track

ORIGIN = "https://music.youtube.com"


def _browser_auth(session: requests.Session) -> dict[str, str]:
    """ytmusicapi browser-auth headers built from the account cookie session.

    Library endpoints require the signed-in account: the cookie header plus
    an origin lets ytmusicapi treat the client as browser-authenticated.
    Only YouTube-scoped cookies are sent — that is exactly what a browser
    sends to music.youtube.com, and it keeps the SAPISID extraction
    unambiguous. The authorization header carries a real SAPISIDHASH (the
    web app computes the same value): YouTube ignores a placeholder here
    and answers every authenticated endpoint as anonymous.
    """
    from ytmusicapi.helpers import get_authorization

    youtube_cookies = []
    sapisid = None
    for cookie in session.cookies:
        if str(cookie.domain).endswith(".youtube.com"):
            youtube_cookies.append(cookie)
            if cookie.name in ("SAPISID", "__Secure-3PAPISID") and sapisid is None:
                sapisid = cookie.value
    cookie_header = "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in youtube_cookies
    )
    return {
        "cookie": cookie_header,
        "origin": ORIGIN,
        "authorization": get_authorization(f"{sapisid} {ORIGIN}"),
        "x-goog-authuser": "0",
        "x-origin": ORIGIN,
    }


def _account_session(cookies: Cookies) -> requests.Session:
    """A session carrying the *full* YouTube cookie set.

    Library endpoints need every auth cookie (SAPISID, SID, __Secure-3PSID,
    …), not just the streaming subset a hand-written cookie file often holds,
    so browser cookies are extracted with yt-dlp when no file is given.
    """
    if cookies.cookiefile:
        return cookies.requests_session()  # type: ignore[return-value]
    browser, *rest = cookies.cookiesfrombrowser or (None,)
    profile, keyring, container = (*rest, None, None, None)[:3]
    try:
        jar = extract_cookies_from_browser(
            browser, profile, keyring=keyring, container=container
        )
    except Exception as error:
        raise PlayerError(
            f"Could not extract YouTube cookies from {browser}: {error}"
        ) from error
    session = requests.Session()
    session.cookies = jar
    session.headers.update({"User-Agent": USER_AGENT})
    return session


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

    Authenticates with browser cookies by default (captured by
    ``music-cli login``), or an OAuth token file when configured.
    """

    def __init__(
        self,
        api: YTMusic | None = None,
        cookies: Cookies | None = None,
        *,
        oauth_file: str | None = None,
        limit: int = 25,
    ) -> None:
        self._api = api
        self._cookies = cookies
        self._oauth_file = oauth_file
        self._limit = limit

    @property
    def authenticated(self) -> bool:
        """Whether account credentials are configured for library access."""
        return bool(self._oauth_file or (self._cookies and self._cookies.enabled))

    def _client(self) -> YTMusic:
        if self._api is not None:
            return self._api
        if self._oauth_file:
            return YTMusic(
                auth=self._oauth_file,
                oauth_credentials=self._oauth_credentials(),
            )
        if self._cookies and self._cookies.enabled:
            session = _account_session(self._cookies)
            return YTMusic(auth=_browser_auth(session), requests_session=session)
        return YTMusic()

    def _oauth_credentials(self) -> OAuthCredentials:
        """Load the OAuth client credentials stored alongside the token."""
        if not self._oauth_file:
            raise PlayerError("No OAuth file configured")
        path = Path(self._oauth_file)
        if not path.is_file():
            raise PlayerError(f"OAuth file not found: {self._oauth_file}")
        data = json.loads(path.read_text(encoding="utf-8"))
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        if not client_id or not client_secret:
            raise PlayerError(
                f"{self._oauth_file} is missing client_id/client_secret; "
                "re-run 'music-cli oauth' to regenerate it"
            )
        return OAuthCredentials(client_id, client_secret)

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


def run_oauth_setup(client_id: str, client_secret: str, filepath: str) -> None:
    """Authorize the app with a YouTube Music account and persist the token.

    Runs ytmusicapi's interactive device flow, then stores the OAuth client
    credentials alongside the token so the library can refresh it later
    without further user input.
    """
    ytm_setup_oauth(client_id, client_secret, filepath=filepath, open_browser=True)
    path = Path(filepath)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["client_id"] = client_id
    data["client_secret"] = client_secret
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
