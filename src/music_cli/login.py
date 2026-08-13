"""Browser sign-in: capture YouTube Music account cookies with Playwright.

Google's own OAuth requires per-app client credentials from the Google Cloud
console, which is unreasonable to ask of an open-source CLI's users. Instead
a real browser (Playwright Chromium) is opened on music.youtube.com, the user
signs in through Google's ordinary login page, and the resulting account
cookies are saved as a Netscape-format cookie file. That single file then
powers every authenticated feature: yt-dlp stream extraction and ytmusicapi
library playlists (browser auth) alike.

Playwright is imported lazily so the app still runs (anonymously) when it is
not installed; the browser binary is downloaded on first sign-in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from .player import Cookies, PlayerError

SIGNIN_URL = "https://music.youtube.com/"

AUTH_COOKIE_NAMES = frozenset(
    {
        "SAPISID",
        "SID",
        "HSID",
        "SSID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-3PAPISID",
    }
)

CONFIG_DIR_NAME = "music-cli"
COOKIE_FILENAME = "cookies.txt"
BROWSER_PROFILE_DIRNAME = "browser-profile"
SKIP_AUTH_MARKER_NAME = "no-sign-in"

StatusCallback = Callable[[str], None]


@dataclass(frozen=True)
class LoginResult:
    """The outcome of a successful browser sign-in."""

    cookie_path: Path
    playlist_count: int | None


def config_dir() -> Path:
    """The music-cli config directory (``$MUSIC_CLI_CONFIG_DIR`` or XDG)."""
    override = os.environ.get("MUSIC_CLI_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / CONFIG_DIR_NAME


def default_cookie_path() -> Path:
    """Where browser sign-in saves cookies; ``$MUSIC_CLI_COOKIE_FILE`` wins."""
    override = os.environ.get("MUSIC_CLI_COOKIE_FILE")
    if override:
        return Path(override)
    return config_dir() / COOKIE_FILENAME


def browser_profile_dir() -> Path:
    """The persistent browser profile, so re-signing in stays logged in."""
    return config_dir() / BROWSER_PROFILE_DIRNAME


def skip_auth_marker() -> Path:
    """Marker file recording that the user declined sign-in onboarding."""
    return config_dir() / SKIP_AUTH_MARKER_NAME


def auth_was_skipped() -> bool:
    return skip_auth_marker().is_file()


def mark_auth_skipped() -> None:
    path = skip_auth_marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def clear_auth_skipped() -> None:
    skip_auth_marker().unlink(missing_ok=True)


def auth_cookies_present(cookies: Iterable[Mapping[str, object]]) -> bool:
    """Whether ``cookies`` carry a signed-in YouTube account."""
    names = {cookie.get("name") for cookie in cookies}
    return bool(names & AUTH_COOKIE_NAMES)


def to_netscape_cookie(raw: Mapping[str, object]) -> Cookie:
    """Convert one Playwright cookie dict to an ``http.cookiejar`` cookie.

    Playwright reports ``expires`` as -1 for session cookies and
    ``sameSite`` as one of ``"Strict"``/``"Lax"``/``"None"``, which the
    cookiejar's ``same_site`` attribute is not directly compatible with.
    """
    expires = raw.get("expires")
    value = raw.get("value")
    cookie = Cookie(
        version=0,
        name=str(raw["name"]),
        value="" if value is None else str(value),
        port=None,
        port_specified=False,
        domain=str(raw["domain"]),
        domain_specified=bool(raw.get("domain")),
        domain_initial_dot=str(raw.get("domain", "")).startswith("."),
        path=str(raw.get("path") or "/"),
        path_specified=bool(raw.get("path")),
        secure=bool(raw.get("secure")),
        expires=int(expires)
        if isinstance(expires, (int, float)) and expires > 0
        else None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": bool(raw.get("httpOnly"))},
        rfc2109=False,
    )
    return cookie


def save_cookie_file(cookies: Iterable[Mapping[str, object]], path: Path) -> None:
    """Write Playwright cookies to ``path`` in Netscape format.

    The same format yt-dlp's ``cookiefile`` and ``Cookies.from_file`` read.
    """
    jar = MozillaCookieJar()
    for raw in cookies:
        if raw.get("domain") and str(raw["domain"]).endswith(
            (".youtube.com", ".google.com")
        ):
            jar.set_cookie(to_netscape_cookie(raw))
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(path), ignore_discard=True, ignore_expires=True)


def browser_login(
    output_path: Path | str,
    *,
    status: StatusCallback | None = None,
    poll_seconds: float = 2.0,
) -> LoginResult:
    """Sign in with a browser and persist the account cookies.

    Opens a headed Chromium on music.youtube.com and waits until the account
    cookies appear, then saves them to ``output_path`` and verifies the
    library is reachable with them.

    Raises ``PlayerError`` when Playwright or the browser are missing, the
    browser was closed before signing in, or the captured cookies fail the
    library check. Ctrl+C aborts, closing the browser.
    """
    report = status or (lambda text: None)
    target = Path(output_path)
    playwright = _import_playwright()
    with playwright() as api:
        _ensure_browser_binary(api, report)
        report("Opening browser…")
        context = api.chromium.launch_persistent_context(
            user_data_dir=str(browser_profile_dir()),
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Google refuses password entry in browsers it detects as automated.
        # Hide the automation signal from the page before it loads.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(SIGNIN_URL)
            report("Sign in to YouTube Music in the opened browser…")
            cookies = _wait_for_auth(context, poll_seconds)
        finally:
            context.close()
    save_cookie_file(cookies, target)
    report("Signed in — checking your library…")
    count = _verify_library(target)
    clear_auth_skipped()
    report(f"Sign-in saved to {target}")
    return LoginResult(cookie_path=target, playlist_count=count)


def _wait_for_auth(context, poll_seconds: float) -> list[dict[str, object]]:
    """Poll the browser context until a signed-in account is detected."""
    while True:
        try:
            cookies = context.cookies()
        except Exception as error:
            raise PlayerError("Browser was closed before signing in") from error
        if auth_cookies_present(cookies):
            return cookies
        time.sleep(poll_seconds)


def _verify_library(cookie_path: Path) -> int | None:
    """Confirm the captured cookies unlock the library, returning its size.

    A browser profile can carry stale cookies that still look signed-in but
    no longer authenticate; the library call catches that before the app
    tells the user everything worked.
    """
    from .playlists import Library

    cookies = Cookies.from_file(str(cookie_path))
    try:
        return len(Library(cookies=cookies).playlists())
    except Exception as error:
        raise PlayerError(
            "Sign-in was detected but the library check failed "
            f"({error}); try 'music-cli login' again"
        ) from error


def _import_playwright():
    """Import Playwright's sync API, with a friendly hint when missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PlayerError(
            "playwright is not installed; run 'pip install playwright' "
            "to enable browser sign-in"
        ) from None
    return sync_playwright


def _ensure_browser_binary(api, report: StatusCallback) -> None:
    """Download the Chromium browser on first sign-in."""
    from contextlib import suppress

    from playwright._impl._errors import Error as PlaywrightError

    with suppress(PlaywrightError):
        probe = api.chromium.launch(headless=True)
        probe.close()
        return
    report("Downloading the browser (one-time, ~150 MB)…")
    try:
        from playwright._impl._driver import compute_driver_executable

        command = [compute_driver_executable(), "install", "chromium"]
    except ImportError:
        command = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        subprocess.run(command, check=True)  # noqa: S603 - fixed arg list
    except (OSError, subprocess.CalledProcessError) as error:
        raise PlayerError(f"Could not download the browser: {error}") from error
