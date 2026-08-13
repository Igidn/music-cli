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

GOOGLE_ONLY_COOKIES = [
    {"name": "SAPISID", "value": "abc", "domain": ".google.com", "path": "/"},
    {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
]


def test_auth_cookies_present():
    assert auth_cookies_present(SIGNED_IN_COOKIES)
    assert not auth_cookies_present(ANONYMOUS_COOKIES)
    assert not auth_cookies_present([])
    # Google sets SAPISID on .google.com mid-login; only the .youtube.com
    # copies authenticate music.youtube.com.
    assert not auth_cookies_present(GOOGLE_ONLY_COOKIES)


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
            {
                "name": "IRRELEVANT",
                "value": "x",
                "domain": ".example.com",
                "path": "/",
            },
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


def _setup(tmp_path, monkeypatch, cookies_fn):
    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    context = FakeContext(cookies_fn)
    fake = FakeSyncPlaywright(FakeChromium(context))
    monkeypatch.setattr(login, "_import_playwright", lambda: fake)
    return context


def test_browser_login_saves_and_verifies(tmp_path, monkeypatch):
    polls = []

    def cookies_fn():
        polls.append(1)
        if len(polls) < 3:
            return ANONYMOUS_COOKIES
        return SIGNED_IN_COOKIES

    _setup(tmp_path, monkeypatch, cookies_fn)
    monkeypatch.setattr(login, "_verify_library", lambda path: ("Igidn", 3))

    output = tmp_path / "out" / "cookies.txt"
    statuses = []
    result = browser_login(output, status=statuses.append, poll_seconds=0)

    assert isinstance(result, LoginResult)
    assert result.cookie_path == output
    assert result.account_name == "Igidn"
    assert result.playlist_count == 3
    assert output.is_file()
    assert statuses
    assert not any("didn't take effect" in text for text in statuses)


def test_browser_login_waits_out_stale_session(tmp_path, monkeypatch):
    """Stale cookies fail verification once, then a real sign-in passes."""
    stale = [
        dict(cookie, value=cookie["value"] + "-stale") for cookie in SIGNED_IN_COOKIES
    ]
    polls = []
    verify_calls = []

    def cookies_fn():
        polls.append(1)
        if len(polls) <= 2:
            return stale
        return SIGNED_IN_COOKIES

    _setup(tmp_path, monkeypatch, cookies_fn)

    def flaky_verify(path):
        verify_calls.append(1)
        if len(verify_calls) == 1:
            raise PlayerError("library rejected")
        return "Igidn", 3

    monkeypatch.setattr(login, "_verify_library", flaky_verify)

    output = tmp_path / "cookies.txt"
    statuses = []
    result = browser_login(output, status=statuses.append, poll_seconds=0)

    assert result.cookie_path == output
    assert any("didn't take effect" in text for text in statuses)
    assert len(verify_calls) == 2
    assert output.is_file()


def test_browser_login_wipes_previous_profile(tmp_path, monkeypatch):
    """A stale profile is deleted so the sign-in starts from a clean slate."""
    profile = tmp_path / "browser-profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("stale session")
    stale_marker = profile / "Local State"
    stale_marker.write_text("{}")

    def cookies_fn():
        return SIGNED_IN_COOKIES

    _setup(tmp_path, monkeypatch, cookies_fn)
    monkeypatch.setattr(login, "_verify_library", lambda path: ("Igidn", 3))

    output = tmp_path / "cookies.txt"
    browser_login(output, poll_seconds=0)

    assert not profile.exists()
    assert output.is_file()


def test_browser_login_browser_closed_mid_wait(tmp_path, monkeypatch):
    def closed():
        raise RuntimeError("Target page, context or browser has been closed")

    _setup(tmp_path, monkeypatch, closed)
    with pytest.raises(PlayerError, match="closed"):
        browser_login(tmp_path / "cookies.txt", poll_seconds=0)


def test_no_message_without_auth_cookies(tmp_path, monkeypatch):
    """A fresh profile (no auth cookies) waits quietly for a sign-in."""
    _setup(tmp_path, monkeypatch, lambda: ANONYMOUS_COOKIES)
    sleeps = []

    def interrupted(*args):
        sleeps.append(1)
        if len(sleeps) > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(login.time, "sleep", interrupted)
    statuses = []
    with pytest.raises(KeyboardInterrupt):
        login._wait_for_verified_auth(
            FakeContext(lambda: ANONYMOUS_COOKIES), statuses.append, 2.0
        )
    assert statuses == []


def test_auth_signature_ignores_non_auth_cookies():
    signature = login._auth_signature(
        [
            *SIGNED_IN_COOKIES,
            {"name": "PREF", "value": "zz", "domain": ".youtube.com"},
        ]
    )
    assert signature == login._auth_signature(SIGNED_IN_COOKIES)
    assert signature != login._auth_signature(
        [dict(cookie, value="other") for cookie in SIGNED_IN_COOKIES]
    )


class _AccountMenuResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _AccountMenuSession:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.headers = None

    def post(self, *args, **kwargs):
        self.headers = kwargs.get("headers")
        return _AccountMenuResponse(self._payload)


def _account_menu_payload(account_name):
    return {
        "actions": [
            {
                "openPopupAction": {
                    "popup": {
                        "multiPageMenuRenderer": {
                            "header": {
                                "activeAccountHeaderRenderer": {
                                    "accountName": account_name,
                                }
                            },
                            "sections": [
                                {"multiPageMenuSectionRenderer": {"items": []}}
                            ],
                        }
                    }
                }
            }
        ]
    }


def _verify_setup(tmp_path, monkeypatch, payload):
    from music_cli import playlists as playlists_module

    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    cookie_path = tmp_path / "cookies.txt"
    save_cookie_file(SIGNED_IN_COOKIES, cookie_path)
    session = _AccountMenuSession(payload)

    class _FakeApi:
        def __init__(self, cookies=None) -> None:
            self.cookies = cookies

        def playlists(self, limit=25):
            return ["p1", "p2", "p3"]

    monkeypatch.setattr(playlists_module, "_account_session", lambda cookies: session)
    monkeypatch.setattr(playlists_module, "_browser_auth", lambda s: {"auth": "signed"})
    monkeypatch.setattr(playlists_module, "Library", _FakeApi)
    return cookie_path, session


def test_verify_library_parses_account_name_from_runs(tmp_path, monkeypatch):
    payload = _account_menu_payload({"runs": [{"text": "agwjbgsgjk"}]})
    cookie_path, session = _verify_setup(tmp_path, monkeypatch, payload)

    name, count = login._verify_library(cookie_path)

    assert name == "agwjbgsgjk"
    assert count == 3
    assert session.headers == {"auth": "signed"}


def test_verify_library_accepts_plain_account_name(tmp_path, monkeypatch):
    cookie_path, _ = _verify_setup(
        tmp_path, monkeypatch, _account_menu_payload("Plain Name")
    )
    assert login._verify_library(cookie_path) == ("Plain Name", 3)


def test_verify_library_rejects_anonymous_menu(tmp_path, monkeypatch):
    cookie_path, _ = _verify_setup(tmp_path, monkeypatch, {"actions": []})
    with pytest.raises(PlayerError, match="library rejected"):
        login._verify_library(cookie_path)
