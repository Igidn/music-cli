# music-cli

Ad-free YouTube Music player for the terminal.

## Install

```sh
uv tool install music-cli   # or: pip install music-cli
```

Requires Python 3.14+ and macOS (playback uses AVFoundation).

## Sign in

Run `music-cli login` to sign in. A browser window opens on
music.youtube.com, you sign in with your Google account, and the window
closes itself. The account cookies are saved to
`~/.config/music-cli/cookies.txt` and unlock:

- your library playlists (sidebar)
- account-aware stream extraction

No Google OAuth client setup required. Re-run `music-cli login` when the
cookies expire. Without a sign-in, the TUI runs anonymously and the
playlist panel shows the login command.

The browser is downloaded once on the first sign-in (~150 MB).

### Alternatives

- `--cookies FILE` / `$MUSIC_CLI_COOKIE_FILE` — use a cookie file you
  exported yourself (Netscape format).

## Usage

```sh
music-cli                 # start the TUI
music-cli login           # browser sign-in
music-cli --clear-cache   # delete the audio download cache
```

The last track you played is remembered and resumes automatically the next
time you start the TUI. Player preferences (volume, muted, loop) are
persisted too — once you've changed them, they win over `--volume`.

### Keys

| Key | Action |
| --- | --- |
| `/` | search |
| `enter` | play / open playlist |
| `space` | play / pause |
| `n` | next |
| `a` | toggle auto-next |
| `l` | toggle loop |
| `ctrl+left/right` | seek |
| `+` / `-` | volume |
| `m` | mute |
| `q` | quit |

With a song selected (search results, up next or a playlist's tracks) `s`
opens a picker to add it to playlists. In the playlists panel, `c` creates a
playlist, `r` renames the selected playlist and `d` removes the selected song
from its playlist. These keys appear in the footer only when they apply.

## CLI

Playback commands talk to a background daemon over a control socket. The
daemon is not an always-on service: any `music-cli` command that touches
playback (or opening the TUI) starts it on demand, and it exits automatically
after roughly 30 seconds of idle. `status`, `queue`, `search`,
`playlists` and `history` accept `--json` for machine-readable output.

```sh
music-cli play "never gonna give you up"   # search and play
music-cli play --video-id dQw4w9WgXcQ      # play a video id
music-cli play --playlist PL... [--loop] [--no-auto-next] [--volume 65]
music-cli pause|resume|toggle|next|stop    # transport; stop halts playback
                                           # and the daemon exits on its own
music-cli seek +30 | seek -10 | seek 90    # forward / back / absolute
music-cli volume 65 | volume +5            # absolute / relative
music-cli mute on|off|toggle               # also: loop, auto-next
music-cli status                           # what is playing
music-cli queue                            # the up-next queue
music-cli search QUERY [--limit N] [--filter songs|videos|albums|artists|playlists]
music-cli playlists list                   # also: tracks/create/rename/add/remove
music-cli history [--limit N]              # recently played tracks
```
