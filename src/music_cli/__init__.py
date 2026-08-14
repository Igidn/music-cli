"""music-cli: an ad-free YouTube Music player for the terminal."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .client import MusicClient
from .core.errors import PlayerError
from .storage.cache import AudioCache
from .yt.cookies import Cookies
from .yt.login import default_cookie_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-cli",
        description="Ad-free YouTube Music player for the terminal.",
    )
    parser.add_argument(
        "--cookies",
        metavar="FILE",
        default=os.environ.get("MUSIC_CLI_COOKIE_FILE")
        or _existing_default_cookie_file(),
        help="Netscape-format cookie file for account-aware playback "
        "(default: $MUSIC_CLI_COOKIE_FILE, or the file created by "
        "'music-cli login')",
    )
    parser.add_argument(
        "--volume",
        type=int,
        default=80,
        help="initial volume, 0-100 (default: 80)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the audio download cache and exit",
    )
    parser.add_argument(
        "--cache-size-mb",
        type=int,
        metavar="MB",
        default=None,
        help="max size of the audio download cache in MiB "
        "(default: 2048, or $MUSIC_CLI_CACHE_SIZE_MB)",
    )
    subparsers = parser.add_subparsers(dest="command")
    login_parser = subparsers.add_parser(
        "login",
        help="sign in with your YouTube Music account using your browser",
    )
    login_parser.add_argument(
        "--file",
        metavar="FILE",
        default=None,
        help="where to save the cookies (default: $MUSIC_CLI_COOKIE_FILE "
        "or ~/.config/music-cli/cookies.txt)",
    )
    from .cli import register_subcommands

    register_subcommands(subparsers)
    return parser


def _existing_default_cookie_file() -> str | None:
    path = default_cookie_path()
    return str(path) if path.is_file() else None


def build_client(args: argparse.Namespace) -> MusicClient:
    cookies = None
    if args.cookies:
        if not os.path.isfile(args.cookies):
            raise PlayerError(f"Cookie file not found: {args.cookies}")
        cookies = Cookies.from_file(args.cookies)
    size_mb = args.cache_size_mb
    if size_mb is None:
        size_mb = int(os.environ.get("MUSIC_CLI_CACHE_SIZE_MB") or "2048")
    cache = AudioCache(max_size=size_mb * 1024 * 1024)
    return MusicClient(cookies=cookies, volume=args.volume, cache=cache)


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "login":
        run_login(args.file)
        return
    if args.clear_cache:
        AudioCache().clear()
        print("music-cli: audio cache cleared")
        return
    if args.command is not None:
        from .cli import run

        sys.exit(run(args))
    try:
        client = build_client(args)
    except PlayerError as error:
        print(f"music-cli: {error}", file=sys.stderr)
        sys.exit(1)
    from . import ipc
    from .tui.app import MusicTUI

    ipc.ensure_daemon(None, args.volume)
    MusicTUI(client).run()


def run_login(filepath: str | None) -> None:
    """CLI entry for 'music-cli login': browser sign-in with progress prints."""
    from .yt.login import browser_login

    output = Path(filepath) if filepath else default_cookie_path()
    try:
        result = browser_login(output, status=lambda text: print(f"music-cli: {text}"))
    except KeyboardInterrupt:
        print("music-cli: sign-in cancelled", file=sys.stderr)
        sys.exit(1)
    except PlayerError as error:
        print(f"music-cli: {error}", file=sys.stderr)
        sys.exit(1)
    name = result.account_name
    count = result.playlist_count
    details = []
    if name:
        details.append(f"as {name}")
    if count:
        details.append(f"{count} playlists found")
    suffix = f" ({', '.join(details)})" if details else ""
    print(f"music-cli: signed in — cookies saved to {result.cookie_path}{suffix}")


if __name__ == "__main__":
    main()
