"""Tests for browser sign-in: cookie capture, conversion and persistence.

The Playwright API is faked so no browser is launched; the live flow is
covered by running 'music-cli login' manually.
"""

from __future__ import annotations

from http.cookiejar import MozillaCookieJar

import pytest

from music_cli import login
from music_cli.login import (
    LoginResult,
    auth_cookies_present,
    browser_login,
    default_cookie_path,
    save_cookie_file,
    to_netscape_cookie,
)
from music_cli.player import PlayerError

SIGNED_IN_COOKIES = [
    {"name": "SAPISID", "value": "abc/xyz", "domain": ".youtube.com", "path": "/"},
    {"name": "SID", "value": "sidvalue", "domain": ".youtube.com", "path": "/"},
    {
        "name": "__Secure-3PSID",
        "value": "securevalue",
        "domain": ".youtube.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": 1900000000,
    },
]

ANONYMOUS_COOKIES = [
    {"name": "PREF", "value": "f6=40000000", "domain": ".youtube.com", "path": "/"},
    {
        "name": "VISITOR_INFO1_LIVE",
        "value": "xx",
        "domain": ".youtube.com",
        "path": "/",
    },
]


def test_auth_cookies_present():
    assert auth_cookies_present(SIGNED_IN_COOKIES)
    assert not auth_cookies_present(ANONYMOUS_COOKIES)
    assert not auth_cookies_present([])


def test_to_netscape_cookie_session_and_expiry():
    session = to_netscape_cookie(
        {
            "name": "SID",
            "value": "s",
            "domain": ".youtube.com",
            "path": "/",
            "expires": -1,
        }
    )
    assert session.expires is None
    assert session.domain == ".youtube.com"
    assert session.domain_initial_dot is True

    persistent = to_netscape_cookie(
        {
            "name": "SID",
            "value": "s",
            "domain": ".youtube.com",
            "path": "/",
            "expires": 1900000000,
            "httpOnly": True,
            "secure": True,
        }
    )
    assert persistent.expires == 1900000000
    assert persistent.secure is True
    assert persistent._rest["HttpOnly"] is True


def test_save_cookie_file_round_trip(tmp_path):
    path = tmp_path / "cookies.txt"
    save_cookie_file(
        [
            *SIGNED_IN_COOKIES,
            {"name": "IRRELEVANT", "value": "x", "domain": ".example.com", "path": "/"},
        ],
        path,
    )
    assert path.is_file()
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    names = {cookie.name for cookie in jar}
    assert "SAPISID" in names
    assert "__Secure-3PSID" in names
    assert "IRRELEVANT" not in names


def test_default_cookie_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("MUSIC_CLI_COOKIE_FILE", raising=False)
    assert default_cookie_path() == tmp_path / "cookies.txt"

    monkeypatch.setenv("MUSIC_CLI_COOKIE_FILE", str(tmp_path / "custom.txt"))
    assert default_cookie_path() == tmp_path / "custom.txt"


def test_skip_auth_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    assert not login.auth_was_skipped()
    login.mark_auth_skipped()
    assert login.auth_was_skipped()
    login.clear_auth_skipped()
    assert not login.auth_was_skipped()


class FakePage:
    def __init__(self) -> None:
        self.url = ""

    def goto(self, url: str) -> None:
        self.url = url


class FakeContext:
    def __init__(self, cookies) -> None:
        self.pages = [FakePage()]
        self._cookies = cookies
        self.closed = False

    def add_init_script(self, script: str) -> None:
        self.init_script = script

    def cookies(self):
        return self._cookies()

    def new_page(self):
        return FakePage()

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self._context = context

    def launch(self, headless=True):
        return _ProbeBrowser()

    def launch_persistent_context(self, **kwargs) -> FakeContext:
        return self._context


class _ProbeBrowser:
    def close(self) -> None:
        pass


class FakeSyncPlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass


def test_browser_login_saves_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    polls = []

    def cookies_fn():
        polls.append(1)
        if len(polls) < 3:
            return ANONYMOUS_COOKIES
        return SIGNED_IN_COOKIES

    context = FakeContext(cookies_fn)
    fake = FakeSyncPlaywright(FakeChromium(context))
    monkeypatch.setattr(login, "_import_playwright", lambda: fake)
    monkeypatch.setattr(login, "_verify_library", lambda path: 3)

    output = tmp_path / "out" / "cookies.txt"
    statuses = []
    result = browser_login(output, status=statuses.append, poll_seconds=0)

    assert isinstance(result, LoginResult)
    assert result.cookie_path == output
    assert result.playlist_count == 3
    assert len(polls) == 3
    assert context.closed
    assert output.is_file()
    assert statuses


def test_browser_login_fails_library_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    context = FakeContext(lambda: SIGNED_IN_COOKIES)
    fake = FakeSyncPlaywright(FakeChromium(context))
    monkeypatch.setattr(login, "_import_playwright", lambda: fake)

    def fail_verification(path):
        raise PlayerError("library rejected")

    monkeypatch.setattr(login, "_verify_library", fail_verification)
    output = tmp_path / "cookies.txt"
    with pytest.raises(PlayerError, match="library"):
        browser_login(output, poll_seconds=0)
    assert context.closed


def test_browser_login_browser_closed_mid_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))

    def closed():
        raise RuntimeError("Target page, context or browser has been closed")

    context = FakeContext(closed)
    fake = FakeSyncPlaywright(FakeChromium(context))
    monkeypatch.setattr(login, "_import_playwright", lambda: fake)
    with pytest.raises(PlayerError, match="closed"):
        browser_login(tmp_path / "cookies.txt", poll_seconds=0)
