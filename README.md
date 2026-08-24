<h2 align="center">music-cli</h2>

<p align="center">An ad-free YouTube Music player for the terminal, built for macOS (for now).</p>

No YouTube Premium needed. music-cli pulls streams straight from YouTube Music and plays them. <br>

music-cli got both TUI and CLI; both of them are in sync, allowing agent control (you can run CLI without the TUI and keeping it run in the background).

## Installation

Requires Python 3.14 or newer and macOS.

On **Linux**, playback additionally needs the GStreamer plugins for MP4/AAC:

```sh
# Fedora / RHEL
sudo dnf install gstreamer1-plugins-good gstreamer1-libav

# Debian / Ubuntu
sudo apt install gst-plugins-good1.0 gst-libav1.0

# Arch
sudo pacman -S gst-plugins-good gst-libav
```

Without them, tracks download to 100% but playback fails immediately with a
decoder error (no MP4 demuxer or AAC decoder).

```sh
cd &&
git clone https://github.com/Igidn/music-cli.git &&
cd music-cli/ && 
uv tool install --python 3.14 .
```

Playwright ships with it (for sign in)

Login to youtube music using:

```sh
music-cli login
```

run `music-cli --help` to get started

## Commands

| Command | Description |
| --- | --- |
| `play` | Play a query, video, or playlist |
| `download <id>` | Download a track for offline listening |
| `playlists downloaded` | List tracks you downloaded for offline use |
| `pause` / `resume` / `toggle` | Transport control |
| `next` / `stop` | Skip ahead / halt playback |
| `seek +30` / `seek 120` | Skip forward or jump to a position |
| `volume 50` / `volume +10` | Set or adjust the volume |
| `mute on` / `loop off` / `auto-next on` | Toggle settings |
| `status` / `queue` | Current track and up-next list |
| `search "..."` | Search YouTube Music |
| `playlists ...` | List, play, and edit playlists |
| `history` | Recently played tracks |

Most list and query commands print human-readable tables; add `--json` for machine output.

## License

MIT. See [LICENSE](LICENSE).
