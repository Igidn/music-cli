"""Headless tests for the first-run onboarding and in-TUI sign-in modal."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_tui import FakeLibrary, make_client

from music_cli import login
from music_cli.login import LoginResult
from music_cli.player import Cookies


def fake_login(output_path, status=None, *, playlist_count=2):
    if status:
        status("signing in…")
    return LoginResult(Path(output_path), playlist_count)


def _run(coro):
    return asyncio.run(coro)


def test_onboarding_skip_marks_and_returns_none(tmp_path, monkeypatch):
    from music_cli.tui.onboarding import OnboardingApp

    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))

    async def scenario():
        app = OnboardingApp(login_fn=fake_login)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#welcome-skip")
            await pilot.pause()
            assert app.return_value is None

    _run(scenario())
    assert login.auth_was_skipped()


def test_onboarding_sign_in_returns_cookie_path(tmp_path, monkeypatch):
    from music_cli.tui.onboarding import OnboardingApp

    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))

    async def scenario():
        app = OnboardingApp(login_fn=fake_login)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#welcome-sign-in")
            for _ in range(10):
                await pilot.pause()
            assert app.return_value == login.default_cookie_path()

    _run(scenario())


def test_onboarding_login_failure_allows_retry(tmp_path, monkeypatch):
    from music_cli.tui.onboarding import LoginScreen, OnboardingApp

    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    attempts = []

    def flaky_login(output_path, status=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise login.PlayerError("no browser")
        return LoginResult(Path(output_path), 1)

    async def scenario():
        app = OnboardingApp(login_fn=flaky_login)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#welcome-sign-in")
            for _ in range(10):
                await pilot.pause()
            assert isinstance(app.screen, LoginScreen)
            await pilot.click("#login-retry")
            for _ in range(10):
                await pilot.pause()
            assert app.return_value == login.default_cookie_path()

    _run(scenario())
    assert attempts == [1, 1]


def test_tui_sign_in_refreshes_auth_and_library(tmp_path, monkeypatch):
    from music_cli.tui.app import LibraryTree, MusicTUI

    monkeypatch.setenv("MUSIC_CLI_CONFIG_DIR", str(tmp_path))
    client = make_client()
    client.library = FakeLibrary(authenticated=True)
    refreshed = []
    client.refresh_auth = lambda cookies: refreshed.append(cookies)

    async def scenario():
        app = MusicTUI(client, login_fn=fake_login)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(5):
                await pilot.pause()
            before = len(client.library.playlist_calls)
            await pilot.press("escape")
            await pilot.press("s")
            for _ in range(10):
                await pilot.pause()
            assert len(refreshed) == 1
            assert isinstance(refreshed[0], Cookies)
            assert refreshed[0].cookiefile == str(login.default_cookie_path())
            assert len(client.library.playlist_calls) > before
            tree = app.query_one(LibraryTree)
            assert tree.root.children

    _run(scenario())
