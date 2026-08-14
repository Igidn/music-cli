"""Modal screens for playlist management."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList

from music_cli.playlists import LibraryPlaylist


class AddToPlaylistScreen(ModalScreen[set[str] | None]):
    """Pick which playlists a track should be added to."""

    BINDINGS: ClassVar = [Binding("escape", "save", show=False)]

    def __init__(self, playlists: list[LibraryPlaylist], track_title: str) -> None:
        super().__init__()
        self._playlists = playlists
        self._track_title = track_title

    def compose(self) -> ComposeResult:
        with Vertical(id="add-playlist-modal") as dialog:
            dialog.border_title = "Add to playlist"
            yield Label(self._track_title, id="add-playlist-modal-title")
            if self._playlists:
                yield SelectionList(
                    *(
                        (playlist.title, playlist.playlist_id)
                        for playlist in self._playlists
                    ),
                    id="add-playlist-modal-select",
                )
                yield Label("space: toggle · esc: done", id="add-playlist-modal-hint")
            else:
                yield Label(
                    "No playlists — sign in or create one first",
                    id="add-playlist-modal-empty",
                )

    def action_save(self) -> None:
        select = self.query_one_optional("#add-playlist-modal-select", SelectionList)
        self.dismiss(set(select.selected) if select is not None else set())


class PlaylistNameScreen(ModalScreen[str | None]):
    """Prompt for a playlist name, used for both create and rename."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss_none", show=False)]

    def __init__(self, prompt: str, *, initial: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="name-modal"):
            yield Label(self._prompt, id="name-modal-title")
            yield Input(
                value=self._initial, placeholder="Playlist name", id="name-modal-input"
            )
            with Horizontal(id="name-modal-buttons"):
                yield Button("Save", variant="primary", id="name-modal-save")
                yield Button("Cancel", id="name-modal-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "name-modal-save":
            self._submit()
        else:
            self.dismiss(None)

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        name = self.query_one(Input).value.strip()
        if name:
            self.dismiss(name)


class ConfirmScreen(ModalScreen[bool | None]):
    """Yes/no confirmation for destructive playlist actions."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss_none", show=False)]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Label(self._message, id="confirm-modal-message")
            with Horizontal(id="confirm-modal-buttons"):
                yield Button("Remove", variant="error", id="confirm-modal-yes")
                yield Button("Cancel", id="confirm-modal-no")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-modal-yes":
            self.dismiss(True)
        else:
            self.dismiss(False)
