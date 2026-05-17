#!/usr/bin/env bash
# Anthill — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh | bash
#
# What this script does:
#   1. Detects OS and required Python (>=3.9)
#   2. Clones anthill-agent to ~/.anthill-agent/
#   3. Sets up an isolated virtualenv
#   4. Installs anthill into the venv
#   5. Drops a wrapper at ~/.local/bin/anthill so `anthill` Just Works
#   6. Prints next-step setup instructions
#
# Designed to be idempotent — re-running upgrades to the latest main.

set -euo pipefail

ANTHILL_DIR="${ANTHILL_INSTALL_DIR:-$HOME/.anthill-agent}"
ANTHILL_REPO="${ANTHILL_REPO:-https://github.com/fengty/anthill-agent.git}"
ANTHILL_BRANCH="${ANTHILL_BRANCH:-main}"
BIN_DIR="${ANTHILL_BIN_DIR:-$HOME/.local/bin}"

# --- pretty output ---
BOLD=$'\033[1m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

say()  { printf "%s\n" "$*"; }
ok()   { printf "${GREEN}✓${RESET} %s\n" "$*"; }
info() { printf "${DIM}→${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }
die()  { printf "${RED}✗${RESET} %s\n" "$*" >&2; exit 1; }

# --- preflight ---
say "${BOLD}Anthill installer${RESET}"
say ""

# Python
for py in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    PYTHON="$py"
    break
  fi
done
[ -z "${PYTHON:-}" ] && die "Python 3.9+ required. Install Python first, then re-run."

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYTHON_OK=$("$PYTHON" -c 'import sys; print("yes" if sys.version_info >= (3, 9) else "no")')
[ "$PYTHON_OK" = "yes" ] || die "Found Python $PYTHON_VERSION, need >=3.9."
ok "Python $PYTHON_VERSION at $(command -v "$PYTHON")"

# Git
command -v git >/dev/null 2>&1 || die "git required."
ok "git at $(command -v git)"

# --- clone or update ---
# Why we DON'T pass --quiet to git/pip below: silent operations look
# identical to a hang. A pip install that's 60 seconds of slow dep
# resolution and a frozen network call are the same to the user. So
# we let the tools print their normal status lines.

# git_with_retry CMD... — try once with default settings, then once
# more forcing HTTP/1.1. The HTTP/2 framing layer error is a known
# flaky network mode that hits some users on certain Wi-Fi / proxies,
# and HTTP/1.1 reliably works around it.
git_with_retry() {
  if git "$@"; then
    return 0
  fi
  warn "git failed (possibly HTTP/2 negotiation). Retrying with HTTP/1.1..."
  git -c http.version=HTTP/1.1 "$@"
}

# When all git attempts have failed, print actionable fallbacks rather
# than a generic "network down". The most common cause we see in
# practice is users behind firewalls / in regions where github.com
# routing is unreliable (e.g. mainland China) — for them, a mirror
# or a proxy is the right answer, not "check your network".
network_die() {
  printf "${RED}✗${RESET} Could not reach %s.\n\n" "$ANTHILL_REPO"
  printf "${BOLD}Common fixes:${RESET}\n\n"
  printf "  ${BOLD}1.${RESET} Use a GitHub mirror (works for users in regions where\n"
  printf "     github.com routing is unreliable):\n\n"
  printf "       ${DIM}ANTHILL_REPO=https://kkgithub.com/fengty/anthill-agent.git \\\\${RESET}\n"
  printf "       ${DIM}  bash <(curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh)${RESET}\n\n"
  printf "  ${BOLD}2.${RESET} Use an HTTPS proxy you already run (clash / v2ray / corp proxy):\n\n"
  printf "       ${DIM}export HTTPS_PROXY=http://127.0.0.1:7890${RESET}\n"
  printf "       ${DIM}curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh | bash${RESET}\n\n"
  printf "  ${BOLD}3.${RESET} Use SSH (if you have an SSH key on your GitHub account):\n\n"
  printf "       ${DIM}ANTHILL_REPO=git@github.com:fengty/anthill-agent.git \\\\${RESET}\n"
  printf "       ${DIM}  bash <(curl -fsSL https://raw.githubusercontent.com/fengty/anthill-agent/main/scripts/install.sh)${RESET}\n\n"
  printf "  ${BOLD}4.${RESET} Clone manually, then run pip install from the cloned dir.\n\n"
  exit 1
}

if [ -d "$ANTHILL_DIR/.git" ]; then
  info "Existing install at $ANTHILL_DIR — fetching latest"
  if ! git_with_retry -C "$ANTHILL_DIR" fetch origin "$ANTHILL_BRANCH"; then
    warn "git fetch failed twice. Attempting clean reclone..."
    rm -rf "$ANTHILL_DIR"
    if ! git_with_retry clone --branch "$ANTHILL_BRANCH" "$ANTHILL_REPO" "$ANTHILL_DIR"; then
      network_die
    fi
  else
    info "Resetting to origin/$ANTHILL_BRANCH"
    git -C "$ANTHILL_DIR" reset --hard "origin/$ANTHILL_BRANCH"
  fi
else
  info "Cloning into $ANTHILL_DIR"
  rm -rf "$ANTHILL_DIR"
  if ! git_with_retry clone --branch "$ANTHILL_BRANCH" "$ANTHILL_REPO" "$ANTHILL_DIR"; then
    network_die
  fi
fi
ok "Source ready at $ANTHILL_DIR"

# --- venv ---
VENV="$ANTHILL_DIR/.venv"
if [ ! -d "$VENV" ]; then
  info "Creating virtualenv"
  "$PYTHON" -m venv "$VENV"
fi
info "Upgrading pip"
"$VENV/bin/pip" install --upgrade pip
info "Installing Anthill + dependencies (this can take 30-60s on first run)"
# We do NOT silence pip — when deps take a minute, the user needs to
# see "Collecting X" rolling by, not an empty stare.
"$VENV/bin/pip" install -e "$ANTHILL_DIR"
ok "Anthill installed into $VENV"

# --- wrapper ---
mkdir -p "$BIN_DIR"
WRAPPER="$BIN_DIR/anthill"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# Auto-generated by anthill installer
exec "$VENV/bin/anthill" "\$@"
EOF
chmod +x "$WRAPPER"
ok "Wrapper at $WRAPPER"

# --- PATH check ---
case ":$PATH:" in
  *":$BIN_DIR:"*) PATH_OK=1 ;;
  *) PATH_OK=0 ;;
esac

say ""
ok "${BOLD}Anthill installed.${RESET}"

if [ "$PATH_OK" != "1" ]; then
  warn "$BIN_DIR is not on your PATH."
  warn "Add this to your shell profile (~/.zshrc or ~/.bashrc):"
  printf "${DIM}    export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}\n"
fi

say ""
say "${BOLD}Next steps:${RESET}"
say "  1. One-shot wizard (interactive):"
say "       anthill setup"
say "  2. ...or configure manually:"
say "       anthill model add deepseek --provider deepseek \\"
say "         --model deepseek-chat --key sk-... --set-default"
say "       anthill init"
say "       anthill spawn --count 4"
say "  3. Give it work:"
say "       anthill ask \"用一句话解释什么是信息素路由\""
say ""
say "${DIM}Keys live in ~/.anthill/secrets.toml (chmod 600). No env vars needed.${RESET}"
say "${DIM}Re-run this installer any time to upgrade to the latest main.${RESET}"
