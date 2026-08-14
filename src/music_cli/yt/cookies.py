"""YouTube account cookie handling and browser-authenticated sessions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from typing import Any

import requests
from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

from ..core.errors import PlayerError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ORIGIN = "https://music.youtube.com"


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
