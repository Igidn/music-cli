"""Messages a track-holder widget posts to the app."""

from __future__ import annotations

from textual.message import Message


class AddToPlaylistRequested(Message):
    """A song is selected in the focused pane and the user pressed `s`.

    Posted by whichever pane holds the selected song (search results,
    up-next queue or the library tree) so the app can open the same
    playlist picker for all three.
    """

    def __init__(self, video_id: str, title: str, artists: tuple[str, ...]) -> None:
        self.video_id = video_id
        self.title = title
        self.artists = artists
        super().__init__()


class QueueAddRequested(Message):
    """The user pressed ctrl+n on a selected track: it should play next.

    Posted by whichever pane holds the selected track (search results,
    library tree or history) so the app sends the same queue_add request
    no matter where the track was picked.
    """

    def __init__(self, video_id: str, title: str) -> None:
        self.video_id = video_id
        self.title = title
        super().__init__()
