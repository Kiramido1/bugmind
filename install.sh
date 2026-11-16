#!/bin/sh
# BugMind installer — user-level only, never uses sudo, never touches system packages.
#
#   curl -fsSL https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/Kiramido1/bugmind/main/install.sh | sh -s -- --uninstall
#   ./install.sh            # from a cloned checkout: installs that checkout (editable)
#
# Options:
#   --ref <branch|tag>   install a specific branch or tag (default: main)
#   --uninstall          remove the bugmind command (your bugs and rules are kept)
#   --no-uv-bootstrap    never download uv; fail instead if neither uv nor pipx is present
set -eu

REPO="Kiramido1/bugmind"
REF="main"
ACTION="install"
BOOTSTRAP=1

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --no-uv-bootstrap) BOOTSTRAP=0; shift ;;
        -h|--help) sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then B="$(printf '\033[1m')"; R="$(printf '\033[31m')"; G="$(printf '\033[32m')"; D="$(printf '\033[2m')"; N="$(printf '\033[0m')"
else B=""; R=""; G=""; D=""; N=""; fi
say()  { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
die()  { printf '%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

case ":$PATH:" in *":$HOME/.local/bin:"*) ON_PATH=1 ;; *) ON_PATH=0 ;; esac
export PATH="$HOME/.local/bin:$PATH"

# A checkout next to this script wins over the GitHub archive (contributors, offline installs).
HERE=""
case "$0" in */install.sh|install.sh) HERE="$(cd "$(dirname "$0")" && pwd)" ;; esac
if [ -n "$HERE" ] && grep -q '^name = "bugmind"' "$HERE/pyproject.toml" 2>/dev/null; then
    SOURCE="$HERE"
else
    SOURCE="bugmind @ https://github.com/$REPO/archive/$REF.tar.gz"
fi

if [ "$ACTION" = "uninstall" ]; then
    if has uv && uv tool list 2>/dev/null | grep -q '^bugmind '; then uv tool uninstall bugmind
    elif has pipx && pipx list --short 2>/dev/null | grep -q '^bugmind '; then pipx uninstall bugmind
    else die "bugmind is not installed with uv or pipx."; fi
    ok "Removed the bugmind command."
    printf '%sYour data was kept: ~/.config/bugmind  ~/.local/share/bugmind  ~/.cache/bugmind%s\n' "$D" "$N"
    exit 0
fi

say "Installing BugMind from ${SOURCE#bugmind @ }"

if ! has uv && ! has pipx; then
    [ "$BOOTSTRAP" = 1 ] || die "Install uv (https://docs.astral.sh/uv/) or pipx first."
    has curl || die "curl is required to download uv."
    say "Installing uv (Astral's Python tool manager) into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    has uv || die "uv installation failed; see https://docs.astral.sh/uv/getting-started/installation/"
fi

if has uv; then
    # uv downloads a managed Python 3.12+ itself when the system one is older.
    if [ "$SOURCE" = "$HERE" ]; then uv tool install --force --editable "$SOURCE"
    else uv tool install --force --reinstall "$SOURCE"; fi
else
    # shellcheck disable=SC2015  # die exits, so this is a guard, not if-then-else
    has python3 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
        || die "BugMind needs Python 3.12+. Install uv (it brings its own Python) and run this again."
    pipx install --force "$SOURCE"
fi

has bugmind || die "Installed, but 'bugmind' is not on PATH. Add ~/.local/bin to PATH and open a new terminal."
ok "$(bugmind --version 2>/dev/null || echo bugmind) installed at $(command -v bugmind)"

echo
say "AI agents found:"
found=0
if has claude; then ok "Claude Code  ($(command -v claude))"; found=1; fi
if has opencode; then ok "OpenCode     ($(command -v opencode))"; found=1; fi
if [ "$found" = 0 ]; then
    printf '  %sNone yet. Install and log in to one of:%s\n' "$R" "$N"
    echo "    Claude Code: https://docs.claude.com/en/docs/claude-code   (then run: claude, /login)"
    echo "    OpenCode:    https://opencode.ai                            (then run: opencode auth login)"
fi

echo
say "Next:"
echo "    bugmind init     # pick the agent, create ~/.config/bugmind"
echo "    bugmind          # open the workspace"
if [ "$ON_PATH" = 0 ]; then
    # shellcheck disable=SC2016  # the $PATH text is meant literally
    printf '%s    (~/.local/bin is not on your PATH yet: echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc)%s\n' "$D" "$N"
fi
