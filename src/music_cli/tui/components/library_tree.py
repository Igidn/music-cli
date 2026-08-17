"""Library playlists sidebar, rendered as a tree."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from music_cli.storage.state import DownloadedTrack
from music_cli.yt.extract import PlaylistTrack
from music_cli.yt.playlists import LibraryPlaylist

from .messages import AddToPlaylistRequested


class LibraryTree(Tree[dict[str, Any] | None], inherit_bindings=False):
    """Library playlists sidebar, rendered as a tree.

    Playlists are branch nodes; activating one lazily fetches its tracks and
    renders them as leaf nodes. Activating a track plays it and queues the
    rest of the playlist. Left/right are left to the app for pane navigation.
    """

    BINDINGS: ClassVar = [
        Binding("enter", "activate", "Open", show=False),
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
        Binding("s", "add_to_playlist", "Add to playlist"),
        Binding("d", "remove_from_playlist", "Remove from playlist"),
        Binding("c", "create_playlist", "Create playlist"),
        Binding("r", "rename_playlist", "Rename playlist"),
    ]

    class DownloadsExpandRequested(Message):
        """The Downloads node was activated while its tracks were still unloaded."""

        def __init__(self, node: TreeNode) -> None:
            self.node = node
            super().__init__()

        @property
        def control(self) -> LibraryTree:
            return self.node.tree

    class DownloadActivated(Message):
        """A downloaded track leaf was activated and should be played."""

        def __init__(self, track: DownloadedTrack) -> None:
            self.track = track
            super().__init__()

    class DownloadRemoveRequested(Message):
        """The user pressed `d` with a downloaded track leaf selected."""

        def __init__(self, track: DownloadedTrack) -> None:
            self.track = track
            super().__init__()

    class PlaylistExpandRequested(Message):
        """A playlist node was activated before its tracks were loaded."""

        def __init__(self, playlist_id: str, node: TreeNode) -> None:
            self.playlist_id = playlist_id
            self.node = node
            super().__init__()

        @property
        def control(self) -> LibraryTree:
            return self.node.tree

    class TrackActivated(Message):
        """A track leaf was activated and should be played."""

        def __init__(
            self,
            playlist_id: str,
            index: int,
            track: PlaylistTrack,
        ) -> None:
            self.playlist_id = playlist_id
            self.index = index
            self.track = track
            super().__init__()

    class TrackRemoveRequested(Message):
        """The user pressed `d` with a track leaf selected."""

        def __init__(self, playlist_id: str, track: PlaylistTrack) -> None:
            self.playlist_id = playlist_id
            self.track = track
            super().__init__()

    class CreatePlaylistRequested(Message):
        """The user pressed `c` while the library tree is focused."""

    class RenamePlaylistRequested(Message):
        """The user pressed `r` with a playlist node selected."""

        def __init__(self, playlist_id: str, title: str) -> None:
            self.playlist_id = playlist_id
            self.title = title
            super().__init__()

    def on_mount(self) -> None:
        self.border_title = " PLAYLISTS "
        self.show_root = False
        self._last_kind: str | None = None
        self.root.add_leaf("Loading library…")
        self._ensure_downloads_node()

    def _ensure_downloads_node(self) -> TreeNode:
        """The always-present Downloads branch (child 0, never removed).

        A local, non-synced pseudo-playlist listing tracks downloaded for
        offline use. It is created first so it always sits above the synced
        library playlists; ``set_playlists``/``set_unavailable`` preserve it.
        """
        for node in self.root.children:
            data = node.data
            if isinstance(data, dict) and data.get("kind") == "downloads":
                return node
        node = self.root.add(
            Text("⬇ Downloads"),
            data={"kind": "downloads", "loaded": False, "loading": False},
            allow_expand=False,
        )
        self._last_kind = None
        self.refresh_bindings()
        return node

    @property
    def downloads_node(self) -> TreeNode:
        return self._ensure_downloads_node()

    @property
    def downloads_loaded(self) -> bool:
        return bool(self.downloads_node.data.get("loaded")) if isinstance(
            self.downloads_node.data, dict
        ) else False

    def update_downloads_count(self, count: int) -> None:
        """Refresh just the Downloads node's label, keeping its collapse state."""
        node = self.downloads_node
        suffix = f" ({count})" if count else ""
        node.set_label(Text(f"⬇ Downloads{suffix}"))
    def set_playlists(self, playlists: list[LibraryPlaylist]) -> None:
        downloads = self.downloads_node
        for node in list(self.root.children):
            if node is not downloads:
                node.remove()
        if not playlists:
            self.root.add_leaf("No playlists in your library")
            return
        for playlist in playlists:
            self.root.add(
                self._playlist_label(playlist.title, playlist.track_count),
                data={
                    "kind": "playlist",
                    "playlist_id": playlist.playlist_id,
                    "title": playlist.title,
                    "track_count": playlist.track_count,
                    "loaded": False,
                },
                allow_expand=True,
            )
        self.refresh_bindings()

    def set_unavailable(self, message: str) -> None:
        """Replace the synced tree contents with a notice, keeping Downloads."""
        downloads = self.downloads_node
        for node in list(self.root.children):
            if node is not downloads:
                node.remove()
        self.root.add_leaf(message)
        self.refresh_bindings()

    def show_downloads(self, tracks: list[DownloadedTrack]) -> None:
        """Fill the Downloads node with downloaded tracks and a count badge."""
        node = self.downloads_node
        node.data["loaded"] = True
        node.data["loading"] = False
        self.update_downloads_count(len(tracks))
        node.remove_children()
        if not tracks:
            node.allow_expand = False
            node.add_leaf("No downloads yet")
            return
        node.allow_expand = True
        for track in tracks:
            node.add_leaf(
                self._download_label(track),
                data={"kind": "download", "track": track},
            )
        self.refresh_bindings()

    @staticmethod
    def _download_label(track: DownloadedTrack) -> Text:
        artists = " • ".join(track.artists) or "Unknown artist"
        return Text.assemble(
            Text(f"⬇ {track.title}"),
            Text(f"  {artists}", style="grey58"),
        )

    def selected_track(self) -> tuple[str, str, str] | None:
        """The (video_id, title, artists) under the cursor, if it is a track."""
        data = self._cursor_data()
        if data is None:
            return None
        if data.get("kind") == "track":
            track = data["track"]
            return track.video_id, track.title, " • ".join(track.artists)
        if data.get("kind") == "download":
            track = data["track"]
            return track.video_id, track.title, " • ".join(track.artists)
        return None

    def show_tracks(self, playlist_id: str, tracks: list[PlaylistTrack]) -> None:
        """Fill ``playlist_id``'s node with its tracks and expand it."""
        node = self._find_playlist(playlist_id)
        if node is None:
            return
        node.data["loaded"] = True
        node.data["loading"] = False
        node.remove_children()
        node.label = self._playlist_label(node.data["title"], node.data["track_count"])
        if not tracks:
            node.allow_expand = False
            node.add_leaf("Empty playlist")
            return
        for index, track in enumerate(tracks):
            node.add_leaf(
                self._track_label(track),
                data={
                    "kind": "track",
                    "playlist_id": playlist_id,
                    "index": index,
                    "track": track,
                },
            )
        node.expand()
        self.refresh_bindings()

    def fail_playlist(self, playlist_id: str) -> None:
        node = self._find_playlist(playlist_id)
        if node is None:
            return
        node.data["loading"] = False
        node.remove_children()
        node.add_leaf("Couldn't load this playlist")
        node.collapse()
        self.refresh_bindings()

    def action_cursor_down(self) -> None:
        """Hand off to the history panel when the cursor sits on the last line."""
        if self.cursor_line >= self.last_line:
            history = self.app.query_one("#history-pane")
            if history.display:
                history.focus()
            return
        super().action_cursor_down()

    def action_activate(self) -> None:
        """Expand/play the cursor node: branch toggles, leaf track plays."""
        node = self.cursor_node
        data = node.data if node is not None else None
        if not isinstance(data, dict):
            return
        if data["kind"] == "track":
            self.post_message(
                self.TrackActivated(data["playlist_id"], data["index"], data["track"])
            )
        elif data["kind"] == "download":
            self.post_message(self.DownloadActivated(data["track"]))
        elif data["kind"] == "downloads":
            if not data["loaded"] and not data.get("loading"):
                data["loading"] = True
                self.post_message(self.DownloadsExpandRequested(node))
            else:
                node.toggle()
        elif not data["loaded"] and not data.get("loading"):
            data["loading"] = True
            self.post_message(self.PlaylistExpandRequested(data["playlist_id"], node))
        else:
            node.toggle()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Only show playlist-management keybinds for the relevant cursor node."""
        kind = self._cursor_kind()
        if action == "create_playlist":
            # Also visible on the empty-library leaf, so a fresh account
            # can create its first playlist.
            return self.app.client.library.authenticated and kind != "track"
        if action in ("add_to_playlist", "remove_from_playlist"):
            return self.app.client.library.authenticated and kind == "track"
        if action == "rename_playlist":
            return self.app.client.library.authenticated and kind == "playlist"
        if action == "remove_download":
            return kind == "download"
        return super().check_action(action, parameters)

    def action_remove_download(self) -> None:
        data = self._cursor_data()
        if data is not None and data.get("kind") == "download":
            self.post_message(self.DownloadRemoveRequested(data["track"]))

    def action_add_to_playlist(self) -> None:
        data = self._cursor_data()
        if data is not None and data.get("kind") == "track":
            track = data["track"]
            self.post_message(
                AddToPlaylistRequested(
                    track.video_id, track.title, tuple(track.artists)
                )
            )

    def action_remove_from_playlist(self) -> None:
        data = self._cursor_data()
        if data is not None and data.get("kind") == "track":
            self.post_message(
                self.TrackRemoveRequested(data["playlist_id"], data["track"])
            )

    def action_create_playlist(self) -> None:
        self.post_message(self.CreatePlaylistRequested())

    def action_rename_playlist(self) -> None:
        data = self._cursor_data()
        if data is not None and data.get("kind") == "playlist":
            self.post_message(
                self.RenamePlaylistRequested(data["playlist_id"], data["title"])
            )

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # refresh_bindings() recomposes the whole footer — too costly per
        # arrow-key repeat. The footer only depends on the cursor node kind,
        # so refresh only when it flips (e.g. playlist node → track leaf).
        data = event.node.data
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind != self._last_kind:
            self._last_kind = kind
            self.refresh_bindings()

    def _cursor_data(self) -> dict[str, Any] | None:
        node = self.cursor_node
        data = node.data if node is not None else None
        return data if isinstance(data, dict) else None

    def _cursor_kind(self) -> str | None:
        data = self._cursor_data()
        return data.get("kind") if data is not None else None

    def _find_playlist(self, playlist_id: str) -> TreeNode | None:
        for node in self.root.children:
            data = node.data
            if (
                isinstance(data, dict)
                and data.get("kind") == "playlist"
                and data.get("playlist_id") == playlist_id
            ):
                return node
        return None

    @staticmethod
    def _playlist_label(title: str, track_count: str) -> Text:
        label = Text.assemble(Text(f"♪ {title}", style="bold"))
        if track_count:
            label.append_text(Text(f"  {track_count}", style="grey58"))
        return label

    @staticmethod
    def _track_label(track: PlaylistTrack) -> Text:
        artists = " • ".join(track.artists) or "Unknown artist"
        return Text.assemble(
            Text(f"♪ {track.title}"),
            Text(f"  {artists}", style="grey58"),
        )
