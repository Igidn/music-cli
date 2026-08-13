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

from .paths import config_dir
from .player import Cookies, PlayerError
from .state import STATE_DB_FILENAME, SettingsStore

SIGNIN_URL = "https://music.youtube.com/"

# account_menu POST body; the response holds the signed-in account's name.
ACCOUNT_MENU_URL = (
    "https://music.youtube.com/youtubei/v1/account/account_menu"
    "?alt=json&key=AIzaSyC9XL3ZjWddXyaVXKRd-4DWCPQXxFi9zVk"
)
ACCOUNT_MENU_BODY = {
    "context": {
        "client": {"clientName": "WEB_REMIX", "clientVersion": "1.20240627.01.00"}
    }
}

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

COOKIE_FILENAME = "cookies.txt"
BROWSER_PROFILE_DIRNAME = "browser-profile"
SKIP_AUTH_MARKER_NAME = "no-sign-in"
NO_SIGN_IN_KEY = "no_sign_in"

StatusCallback = Callable[[str], None]


@dataclass(frozen=True)
class LoginResult:
    """The outcome of a successful browser sign-in."""

    cookie_path: Path
    account_name: str | None = None
    playlist_count: int | None = None


def default_cookie_path() -> Path:
    """Where browser sign-in saves cookies; ``$MUSIC_CLI_COOKIE_FILE`` wins."""
    override = os.environ.get("MUSIC_CLI_COOKIE_FILE")
    if override:
        return Path(override)
    return config_dir() / COOKIE_FILENAME


def browser_profile_dir() -> Path:
    """The scratch browser profile used while signing in.

    A fresh profile on every login: YouTube invalidates sessions that were
    created or re-used through a stale profile (an old session's cookies can
    linger and read as signed-in while the server has already revoked them,
    which the account-menu verification then correctly rejects).
    """
    return config_dir() / BROWSER_PROFILE_DIRNAME


def reset_browser_profile() -> None:
    """Delete the scratch browser profile so the next login starts clean."""
    import shutil

    shutil.rmtree(browser_profile_dir(), ignore_errors=True)


def skip_auth_marker() -> Path:
    """Legacy marker file recording that the user declined sign-in onboarding.

    Migrated into the settings database (``no_sign_in``) on first read;
    kept for the one-time import.
    """
    return config_dir() / SKIP_AUTH_MARKER_NAME


def auth_was_skipped() -> bool:
    store = SettingsStore(config_dir() / STATE_DB_FILENAME)
    skipped = store.get_bool(NO_SIGN_IN_KEY)
    marker = skip_auth_marker()
    if marker.is_file() and not skipped:
        store.set(NO_SIGN_IN_KEY, True)
        skipped = True
    if marker.is_file() and skipped:
        marker.unlink(missing_ok=True)
    return skipped


def mark_auth_skipped() -> None:
    SettingsStore(config_dir() / STATE_DB_FILENAME).set(NO_SIGN_IN_KEY, True)
    skip_auth_marker().unlink(missing_ok=True)


def clear_auth_skipped() -> None:
    SettingsStore(config_dir() / STATE_DB_FILENAME).delete(NO_SIGN_IN_KEY)


def auth_cookies_present(cookies: Iterable[Mapping[str, object]]) -> bool:
    """Whether ``cookies`` carry a signed-in YouTube account.

    Only YouTube-scoped cookies count: Google's login pages set ``SAPISID``
    and friends on ``.google.com`` mid-login, but music.youtube.com only
    authenticates with the copies YouTube itself sets on ``.youtube.com``
    once the redirect back completes.
    """
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if domain.endswith(".youtube.com") and cookie.get("name") in AUTH_COOKIE_NAMES:
            return True
    return False


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

    Opens a headed Chromium on music.youtube.com in a *fresh* profile (any
    previous profile is wiped first, so a stale or revoked session can never
    masquerade as signed-in) and keeps it open until the account cookies in
    the browser *verify* against YouTube's account menu API (the same parser
    used at the end). The browser stays open while the user signs in,
    re-verifying whenever the cookies change.

    Raises ``PlayerError`` when Playwright or the browser are missing, the
    browser was closed before signing in, or the captured cookies fail the
    library check. Ctrl+C aborts, closing the browser.
    """
    report = status or (lambda text: None)
    target = Path(output_path)
    playwright = _import_playwright()
    with playwright() as api:
        _ensure_browser_binary(api, report)
        report("Starting a fresh browser session…")
        reset_browser_profile()
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
            cookies, account_name, count = _wait_for_verified_auth(
                context, report, poll_seconds
            )
        finally:
            context.close()
    save_cookie_file(cookies, target)
    clear_auth_skipped()
    report(f"Sign-in saved to {target}")
    return LoginResult(
        cookie_path=target, account_name=account_name, playlist_count=count
    )


def _wait_for_verified_auth(
    context, report: StatusCallback, poll_seconds: float
) -> tuple[list[dict[str, object]], str, int]:
    """Wait until the browser's cookies verify, then return them.

    The account-menu check is the single source of truth: cookie presence
    proves nothing, so every distinct auth-cookie snapshot is verified
    against the API before the browser closes. Verification also runs on
    open, so an already signed-in profile completes instantly. Failed
    verification keeps the browser open and the poll quiet until the
    cookies change again (i.e. the user finishes signing in).
    """
    checked: set[tuple[tuple[str, str], ...]] = set()
    with _temp_dir() as temp:
        while True:
            try:
                cookies = context.cookies()
            except Exception as error:
                raise PlayerError("Browser was closed before signing in") from error
            signature = _auth_signature(cookies)
            if signature and signature not in checked:
                checked.add(signature)
                result = _verify_live(cookies, temp)
                if result is not None:
                    return cookies, result[0], result[1]
                report(
                    "Sign-in didn't take effect yet — finish signing in the browser…"
                )
            time.sleep(poll_seconds)


def _auth_signature(
    cookies: Iterable[Mapping[str, object]],
) -> tuple[tuple[str, str], ...]:
    """The YouTube-scoped auth cookie names+values, as a comparable key."""
    return tuple(
        sorted(
            (str(cookie["name"]), str(cookie["value"]))
            for cookie in cookies
            if str(cookie.get("domain") or "").endswith(".youtube.com")
            and cookie.get("name") in AUTH_COOKIE_NAMES
        )
    )


def _verify_live(
    cookies: Iterable[Mapping[str, object]], temp: Path
) -> tuple[str, int] | None:
    """Verify the live browser cookies, returning (account, playlists).

    Returns None when the cookies don't authenticate yet (an expired or
    mid-login session), keeping the browser open.
    """
    from contextlib import suppress

    candidate = temp / "candidate.txt"
    save_cookie_file(cookies, candidate)
    with suppress(PlayerError, OSError):
        return _verify_library(candidate)
    return None


class _temp_dir:
    """A self-cleaning temporary directory."""

    def __enter__(self) -> Path:
        import tempfile

        self._path = Path(tempfile.mkdtemp(prefix="music-cli-login-"))
        return self._path

    def __exit__(self, *args) -> None:
        import shutil

        shutil.rmtree(self._path, ignore_errors=True)


def _verify_library(cookie_path: Path) -> tuple[str, int]:
    """Confirm the captured cookies unlock the account, not just its page.

    The account menu endpoint is parsed directly (ytmusicapi's own parser
    is a fragile fixed path that blows up on unexpected response shapes);
    it returns the account name only when the cookies truly authenticate.
    """
    from .playlists import Library, _account_session, _browser_auth

    cookies = Cookies.from_file(str(cookie_path))
    try:
        session = _account_session(cookies)
        response = session.post(
            ACCOUNT_MENU_URL,
            headers=_browser_auth(session),
            json=ACCOUNT_MENU_BODY,
            timeout=30,
        )
        response.raise_for_status()
        account_name = _find_first(response.json(), "accountName")
        if isinstance(account_name, dict):
            runs = account_name.get("runs")
            if isinstance(runs, list) and runs:
                account_name = runs[0].get("text")
        account_name = str(account_name or "").strip()
        if not account_name:
            raise PlayerError("library rejected the cookies (no account returned)")
        return account_name, len(Library(cookies=cookies).playlists())
    except PlayerError:
        raise
    except Exception as error:
        detail = str(error).strip()
        if len(detail) > 200:
            detail = detail[:200] + "…"
        raise PlayerError(
            "Sign-in was detected but the library check failed "
            f"({detail}); try 'music-cli login' again"
        ) from error


def _find_first(value: object, key: str) -> object | None:
    """Depth-first search for the first value stored under ``key``."""
    if isinstance(value, dict):
        for dict_key, item in value.items():
            if dict_key == key:
                return item
            found = _find_first(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


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
