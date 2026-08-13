# music-cli

Ad-free YouTube Music player for the terminal.

## Install

```sh
uv tool install music-cli   # or: pip install music-cli
```

Requires Python 3.14+ and macOS (playback uses AVFoundation).

## Sign in

Run the app — on first start an onboarding screen offers **Sign in with
browser**. A browser window opens on music.youtube.com, you sign in with
your Google account, and the window closes itself. The account cookies are
saved to `~/.config/music-cli/cookies.txt` and unlock:

- your library playlists (sidebar)
- account-aware stream extraction

No Google OAuth client setup required. Re-run `music-cli login` to sign in
again (for example when the cookies expire) — the same flow is available
inside the TUI by pressing `s`.

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
time you start the TUI.

### Keys

| Key | Action |
| --- | --- |
| `/` | search |
| `enter` | play / open playlist |
| `p` | playlists pane |
| `space` | play / pause |
| `n` | next |
| `a` | toggle auto-next |
| `l` | toggle loop |
| `s` | sign in / re-sign in |
| `ctrl+left/right` | seek |
| `+` / `-` | volume |
| `m` | mute |
| `q` | quit |
