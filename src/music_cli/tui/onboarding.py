"""First-run onboarding and in-app browser sign-in.

``OnboardingApp`` runs before the main TUI when no authentication is
configured; ``LoginModal`` offers re-sign-in from inside the TUI when the
saved cookies have expired or failed. Both drive ``login.browser_login`` in
a worker thread and report its progress back through status text.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label
from textual.worker import Worker, WorkerState

from music_cli import login
from music_cli.login import LoginResult, default_cookie_path

LoginFunction = Callable[..., LoginResult]

LOGIN_CSS = """
#welcome-card, #login-card {
    width: 70;
    max-width: 90%;
    height: auto;
    border: round $primary;
    background: $surface;
    padding: 1 2;
    margin: 1 2;
}

#welcome-brand {
    text-align: center;
    text-style: bold;
    color: $primary;
    margin-bottom: 1;
}

#welcome-text {
    margin-bottom: 1;
}

#welcome-sub, #welcome-hint {
    color: $text-muted;
    margin-bottom: 1;
}

#welcome-sign-in, #welcome-skip {
    margin: 1 0;
}

#login-title {
    text-align: center;
    text-style: bold;
    margin-bottom: 1;
}

#login-status {
    margin-bottom: 1;
}

#login-error {
    color: $error;
    margin-bottom: 1;
}

#login-buttons {
    height: auto;
    align: center middle;
    margin-top: 1;
}

#login-buttons Button {
    margin: 0 1;
}
"""


class WelcomeScreen(Screen):
    """The first-run landing screen: sign in or continue anonymously."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="welcome-card"):
                yield Label("♪ music-cli", id="welcome-brand")
                yield Label(
                    "Sign in with your YouTube Music account to browse "
                    "your library playlists and get an ad-free stream.",
                    id="welcome-text",
                )
                yield Label(
                    "No Google OAuth setup needed — a browser opens and you "
                    "sign in with your own account.",
                    id="welcome-sub",
                )
                yield Button(
                    "Sign in with browser", id="welcome-sign-in", variant="primary"
                )
                yield Button("Continue anonymously", id="welcome-skip")
                yield Label(
                    "Already have a cookie file? Pass --cookies FILE "
                    "next time you start music-cli.",
                    id="welcome-hint",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "welcome-sign-in":
            self.app.choose_sign_in()
        elif event.button.id == "welcome-skip":
            self.app.choose_anonymous()


class OnboardingApp(App[Path | None]):
    """Pre-TUI onboarding; returns the cookie path, or None when skipped."""

    CSS = LOGIN_CSS
    TITLE = "music-cli"
    BINDINGS: ClassVar = [Binding("q", "quit_app", "Quit", show=False)]

    def __init__(self, login_fn: LoginFunction | None = None) -> None:
        super().__init__()
        self._login_fn = login_fn or login.browser_login

    def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())

    def action_quit_app(self) -> None:
        self.exit(None)

    def choose_sign_in(self) -> None:
        self.push_screen(LoginScreen(self._login_fn), self._on_login_done)

    def choose_anonymous(self) -> None:
        login.mark_auth_skipped()
        self.exit(None)

    def _on_login_done(self, result: LoginResult | None) -> None:
        if result is None:
            return
        self.exit(result.cookie_path)


class LoginScreen(Screen):
    """Runs the browser sign-in flow and shows its progress.

    Dismisses with the ``LoginResult`` on success, or None when the user
    goes back without signing in.
    """

    def __init__(
        self,
        login_fn: LoginFunction,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name, id, classes)
        self._login_fn = login_fn
        self._worker: Worker[LoginResult] | None = None

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="login-card"):
                yield Label("Sign in with browser", id="login-title")
                yield Label("Starting browser sign-in…", id="login-status")
                yield Label("", id="login-error")
                with Horizontal(id="login-buttons"):
                    yield Button("Sign in again", id="login-retry", disabled=True)
                    yield Button("Back", id="login-cancel")

    def on_mount(self) -> None:
        self._start_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-cancel":
            self.dismiss(None)
        elif event.button.id == "login-retry":
            self._start_login()

    def _start_login(self) -> None:
        self.query_one("#login-error", Label).update("")
        self.query_one("#login-retry", Button).disabled = True
        self._worker = self.run_login(self._login_fn)

    @work(thread=True, exit_on_error=False)
    async def run_login(self, login_fn: LoginFunction) -> LoginResult:
        return login_fn(default_cookie_path(), status=self._report_status)

    def _report_status(self, text: str) -> None:
        self.app.call_from_thread(self._set_status, text)

    def _set_status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#login-status", Label).update(text)

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker is not self._worker or not worker.is_finished:
            return
        if worker.state is WorkerState.SUCCESS:
            self.dismiss(worker.result)
            return
        error = worker.error or "unknown error"
        self.query_one("#login-error", Label).update(f"Sign-in failed: {error}")
        self.query_one("#login-status", Label).update("Sign-in failed.")
        self.query_one("#login-retry", Button).disabled = False


class LoginModal(ModalScreen[LoginResult | None]):
    """In-TUI sign-in modal; same flow as ``LoginScreen`` in a popup."""

    CSS = LOGIN_CSS

    def __init__(self, login_fn: LoginFunction | None = None) -> None:
        super().__init__()
        self._login_fn = login_fn or login.browser_login
        self._worker: Worker[LoginResult] | None = None

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="login-card"):
                yield Label("Sign in with browser", id="login-title")
                yield Label("Starting browser sign-in…", id="login-status")
                yield Label("", id="login-error")
                with Horizontal(id="login-buttons"):
                    yield Button("Sign in again", id="login-retry", disabled=True)
                    yield Button("Cancel", id="login-cancel")

    def on_mount(self) -> None:
        self._start_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-cancel":
            self.dismiss(None)
        elif event.button.id == "login-retry":
            self._start_login()

    def _start_login(self) -> None:
        self.query_one("#login-error", Label).update("")
        self.query_one("#login-retry", Button).disabled = True
        self._worker = self.run_login(self._login_fn)

    @work(thread=True, exit_on_error=False)
    async def run_login(self, login_fn: LoginFunction) -> LoginResult:
        return login_fn(default_cookie_path(), status=self._report_status)

    def _report_status(self, text: str) -> None:
        self.app.call_from_thread(self._set_status, text)

    def _set_status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#login-status", Label).update(text)

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker is not self._worker or not worker.is_finished:
            return
        if worker.state is WorkerState.SUCCESS:
            self.dismiss(worker.result)
            return
        error = worker.error or "unknown error"
        self.query_one("#login-error", Label).update(f"Sign-in failed: {error}")
        self.query_one("#login-status", Label).update("Sign-in failed.")
        self.query_one("#login-retry", Button).disabled = False


def run_onboarding(login_fn: LoginFunction | None = None) -> Path | None:
    """Run the first-run onboarding app; returns the cookie path or None."""
    return OnboardingApp(login_fn=login_fn).run()
