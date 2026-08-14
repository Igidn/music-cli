"""Slim header with the brand, tagline and queue count."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label


class TopBar(Widget):
    """Slim header with the brand, tagline and queue count."""

    def compose(self) -> ComposeResult:
        yield Label("♪ music-cli", id="brand")
        yield Label("", id="queue-count")
