"""music-cli: an ad-free YouTube Music player for the terminal."""

from __future__ import annotations

import argparse
import os
import sys

from .client import MusicClient
from .player import Cookies, PlayerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-cli",
        description="Ad-free YouTube Music player for the terminal.",
    )
    parser.add_argument(
        "--cookies",
        metavar="FILE",
        default=os.environ.get("MUSIC_CLI_COOKIE_FILE"),
        help="Netscape-format cookie file for account-aware playback "
        "(default: $MUSIC_CLI_COOKIE_FILE)",
    )
    parser.add_argument(
        "--volume",
        type=int,
        default=80,
        help="initial volume, 0-100 (default: 80)",
    )
    return parser


def build_client(args: argparse.Namespace) -> MusicClient:
    cookies = None
    if args.cookies:
        if not os.path.isfile(args.cookies):
            raise PlayerError(f"Cookie file not found: {args.cookies}")
        cookies = Cookies.from_file(args.cookies)
    return MusicClient(cookies=cookies, volume=args.volume)


def main() -> None:
    args = build_parser().parse_args()
    try:
        client = build_client(args)
    except PlayerError as error:
        print(f"music-cli: {error}", file=sys.stderr)
        sys.exit(1)
    from .tui.app import MusicTUI

    MusicTUI(client).run()


if __name__ == "__main__":
    main()
