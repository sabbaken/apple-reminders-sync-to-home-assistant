#!/bin/bash
# Installer for the Reminders <-> Home Assistant sync.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sabbaken/apple-reminders-sync-to-home-assistant/main/install.sh)"
#
# Installs keith/reminders-cli if it is missing, drops the sync script into
# ~/.local/bin, and hands over to its guided setup, which asks for the Home
# Assistant address and token and then puts the whole thing in the background.
#
# Nothing here needs sudo, and nothing is written outside $HOME.
#
# For testing against a checkout rather than GitHub:
#   RHS_BASE_URL="file://$PWD" ./install.sh

set -euo pipefail

REPO="${RHS_REPO:-sabbaken/apple-reminders-sync-to-home-assistant}"
BRANCH="${RHS_BRANCH:-main}"
BASE_URL="${RHS_BASE_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}"

BIN_DIR="${RHS_BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/reminders-ha-sync"

say() { printf '%s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. is this even a Mac
# ---------------------------------------------------------------------------

[ "$(uname -s)" = "Darwin" ] || fail "this only works on macOS -- Reminders is a macOS app."

# The sync is written against the Python that ships with macOS so that it needs
# no virtualenv. If this is missing, Command Line Tools are not installed.
PYTHON=/usr/bin/python3
if ! "$PYTHON" --version >/dev/null 2>&1; then
    fail "$PYTHON is not working. Install the developer tools with:
    xcode-select --install"
fi

# ---------------------------------------------------------------------------
# 2. reminders-cli
# ---------------------------------------------------------------------------

find_reminders() {
    command -v reminders 2>/dev/null && return 0
    for candidate in /opt/homebrew/bin/reminders /usr/local/bin/reminders; do
        [ -x "$candidate" ] && printf '%s\n' "$candidate" && return 0
    done
    return 1
}

if REMINDERS=$(find_reminders); then
    say "Found reminders-cli at $REMINDERS"
else
    command -v brew >/dev/null 2>&1 || fail "reminders-cli is missing and Homebrew is not
installed, so it cannot be fetched. Install Homebrew first:

    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"

then run this installer again."

    say "Installing reminders-cli (this is what talks to the Reminders app)..."
    brew install keith/formulae/reminders-cli
    REMINDERS=$(find_reminders) || fail "reminders-cli still cannot be found after installing."
fi

# ---------------------------------------------------------------------------
# 3. the sync script
# ---------------------------------------------------------------------------

mkdir -p "$BIN_DIR"
say "Downloading the sync script to $TARGET"
TEMP=$(mktemp)
trap 'rm -f "$TEMP"' EXIT
curl -fsSL "$BASE_URL/reminders_ha_sync.py" -o "$TEMP" \
    || fail "could not download $BASE_URL/reminders_ha_sync.py"

# Refuse to install half a download rather than leave something broken in place.
"$PYTHON" -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TEMP" \
    || fail "the downloaded file is not valid Python -- the download was truncated."

mv "$TEMP" "$TARGET"
chmod 755 "$TARGET"   # mktemp makes it 0600, and `chmod +x` would leave it 0711
trap - EXIT

# ---------------------------------------------------------------------------
# 4. the Reminders permission
# ---------------------------------------------------------------------------

# Ask for it here rather than inside the setup, because the dialog only appears
# for a process in the user's GUI session -- which this script is, and a
# LaunchAgent later is not.
if ! "$REMINDERS" show-lists >/dev/null 2>&1; then
    say ""
    say "macOS is about to ask whether reminders-cli may read your Reminders."
    say "Click Allow -- without it there is nothing to sync."
    say ""
    "$REMINDERS" show-lists >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 5. hand over to the guided setup
# ---------------------------------------------------------------------------

say ""
exec "$TARGET" setup --reminders-binary "$REMINDERS" "$@"
