#!/bin/sh
# music-cli installer
# usage: curl -fsSL https://raw.githubusercontent.com/Igidn/music-cli/main/install.sh | sh

set -eu

REPO="https://github.com/Igidn/music-cli.git"
DIR="$HOME/music-cli"

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    CYAN=$(printf '\033[36m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    RED=$(printf '\033[31m')
    RESET=$(printf '\033[0m')
else
    BOLD=""; DIM=""; CYAN=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step() { printf '\n%s▸ %s%s\n' "$CYAN$BOLD" "$1" "$RESET"; }
ok()   { printf '  %s✔%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '  %s✘ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }

printf '%s' "
${BOLD}  ┌─────────────────────────────────┐
  │      ♫  music-cli installer     │
  └─────────────────────────────────┘${RESET}
${DIM}  ad-free YouTube Music in your terminal${RESET}
"
step "Checking dependencies"

command -v curl >/dev/null 2>&1 || die "curl is required but not installed"
command -v git  >/dev/null 2>&1 || die "git is required but not installed"
ok "curl and git found"

if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version | cut -d' ' -f2) found"
else
    step "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    ok "uv installed"
fi
if [ -d "$DIR/.git" ]; then
    step "Updating existing clone at $DIR"
    git -C "$DIR" pull --ff-only >/dev/null 2>&1 \
        && ok "updated to latest" \
        || warn "pull failed, using existing checkout"
else
    step "Cloning into $DIR"
    git clone --depth 1 "$REPO" "$DIR" >/dev/null 2>&1 || die "git clone failed"
    ok "cloned"
fi

step "Installing music-cli"
( cd "$DIR" && uv tool install --force . ) \
    || die "uv tool install failed"
ok "installed"

if [ "$(uname -s)" = "Linux" ]; then
    step "GStreamer MP4/AAC plugins"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y gst-plugins-good1.0 gst-libav1.0 >/dev/null 2>&1 && ok "installed via apt" || warn "could not install automatically, run: sudo apt install gst-plugins-good1.0 gst-libav1.0"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y gstreamer1-plugins-good gstreamer1-libav >/dev/null 2>&1 && ok "installed via dnf" || warn "could not install automatically, run: sudo dnf install gstreamer1-plugins-good gstreamer1-libav"
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm gst-plugins-good gst-libav >/dev/null 2>&1 && ok "installed via pacman" || warn "could not install automatically, run: sudo pacman -S gst-plugins-good gst-libav"
    else
        warn "unknown package manager, install GStreamer good/libav plugins manually"
    fi
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "$HOME/.local/bin is not on your PATH, add it to your shell profile" ;;
esac

printf '%s' "
${GREEN}${BOLD}  ─────────────────────────────────
   ✔ music-cli is ready
  ─────────────────────────────────${RESET}

  ${BOLD}Next step:${RESET} log in to YouTube Music

    ${CYAN}music-cli login${RESET}

  ${DIM}then play something:${RESET}

    ${CYAN}music-cli play <query>${RESET}
"
