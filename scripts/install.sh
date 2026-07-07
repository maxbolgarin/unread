#!/usr/bin/env bash
# Install the `unread` CLI with one command — no uv required up front.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install.sh | bash
#
# Or, with the script downloaded locally:
#   bash scripts/install.sh
#
# What this does, in order:
#   1. Installs uv if missing (single static binary; manages its own
#      Python 3.11+ — no system Python or virtualenv involved).
#   2. `uv tool install unread` — isolated tool venv, `unread` on PATH.
#      Already installed? Runs `uv tool upgrade unread` instead, so
#      re-running this one-liner is also an update path.
#   3. Makes sure `unread` stays on PATH for future shells.
#   4. Verifies with `unread --version` and prints next steps.
#
# Non-interactive and idempotent: safe under `curl | bash`, safe to
# re-run. macOS and Linux only — on Windows, use PowerShell:
#   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#   uv tool install unread

set -euo pipefail

# ---------------------------------------------------------------------------
# Color helpers (match scripts/install-bot.sh)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_RST=""
fi
ok()   { printf "%s✓%s %s\n" "$C_GREEN" "$C_RST" "$*"; }
warn() { printf "%s!%s %s\n" "$C_YELLOW" "$C_RST" "$*"; }
err()  { printf "%s✖%s %s\n" "$C_RED" "$C_RST" "$*" >&2; }
step() { printf "\n%s→%s %s\n" "$C_DIM" "$C_RST" "$*"; }

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      err "Unknown option: $arg (this script takes no options; see --help)"
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *)
    err "Unsupported OS: $OS — this script covers macOS and Linux only."
    err "On Windows, run in PowerShell:"
    err '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    err "  uv tool install unread"
    err "Details: https://github.com/maxbolgarin/unread/blob/main/docs/install.md"
    exit 1
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  err "curl is required. Install it with your package manager and re-run."
  exit 1
fi

# Remember the PATH the user's shell came in with — used later to decide
# whether future shells need a PATH update (our export below is
# session-only).
ORIG_PATH="$PATH"

# uv's installer defaults to ~/.local/bin (older releases used
# ~/.cargo/bin); make both reachable for the rest of this run so a
# just-installed uv/unread is immediately usable.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv already installed: $(command -v uv)"
else
  step "Installing uv (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if ! command -v uv >/dev/null 2>&1; then
    err "uv installed but not found on PATH. Open a new shell and re-run this script."
    exit 1
  fi
  ok "uv installed: $(command -v uv)"
fi

# ---------------------------------------------------------------------------
# 2. unread (install, or upgrade when already present)
# ---------------------------------------------------------------------------
if uv tool list 2>/dev/null | grep -q '^unread '; then
  step "unread already installed — upgrading to the latest release"
  uv tool upgrade unread
  ok "unread is up to date"
else
  step "Installing unread"
  uv tool install unread
  ok "unread installed"
fi

if ! command -v unread >/dev/null 2>&1; then
  err "unread installed but not found on PATH — expected it in ~/.local/bin."
  err "Open a new shell and run 'unread doctor', or re-run this script."
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. PATH for future shells
# ---------------------------------------------------------------------------
BIN_DIR="$(dirname "$(command -v unread)")"
case ":$ORIG_PATH:" in
  *":$BIN_DIR:"*)
    : # already on the user's regular PATH — nothing to do
    ;;
  *)
    step "Adding $BIN_DIR to your shell PATH"
    if uv tool update-shell >/dev/null 2>&1; then
      warn "PATH updated — restart your shell (or 'source' your shell rc) before running 'unread' directly."
    else
      warn "Could not update your shell config automatically."
      warn "Add this to your shell rc file: export PATH=\"$BIN_DIR:\$PATH\""
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# 4. Verify + next steps
# ---------------------------------------------------------------------------
step "Verifying"
if ! VERSION_OUT="$(unread --version 2>&1)"; then
  err "unread --version failed:"
  err "$VERSION_OUT"
  exit 1
fi
ok "unread ready: $VERSION_OUT"

printf "\n%sNext steps%s\n" "$C_DIM" "$C_RST"
printf "  1. unread init                        %s# pick AI provider, connect Telegram (optional)%s\n" "$C_DIM" "$C_RST"
printf "  2. unread @somegroup --last-days 7    %s# or any URL / file / YouTube link%s\n" "$C_DIM" "$C_RST"
printf "\n"
printf "%sOptional:%s ffmpeg enables audio/video transcription (e.g. 'brew install ffmpeg' / 'sudo apt install ffmpeg').\n" "$C_DIM" "$C_RST"
printf "%sLater:%s 'unread update' upgrades to new releases.\n" "$C_DIM" "$C_RST"
