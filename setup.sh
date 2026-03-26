#!/usr/bin/env bash
# MailRelay — virtual environment setup script
# Creates .venv, upgrades pip, and installs all required dependencies.
# Run once before first use:  bash setup.sh

set -euo pipefail

VENV_DIR=".venv"
PYTHON="${PYTHON:-python3}"
MIN_PYTHON_MINOR=11   # 3.11+

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Python version check
# ---------------------------------------------------------------------------

if ! command -v "$PYTHON" &>/dev/null; then
    die "'$PYTHON' not found. Install Python 3.11+ and retry, or set PYTHON=/path/to/python3."
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MINOR=$("$PYTHON"  -c 'import sys; print(sys.version_info.minor)')
PY_MAJOR=$("$PYTHON"  -c 'import sys; print(sys.version_info.major)')

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt "$MIN_PYTHON_MINOR" ) ]]; then
    die "Python 3.${MIN_PYTHON_MINOR}+ required (found $PY_VERSION)."
fi

green "Python $PY_VERSION — OK"

# ---------------------------------------------------------------------------
# Create virtual environment
# ---------------------------------------------------------------------------

if [[ -d "$VENV_DIR" ]]; then
    yellow "Virtual environment already exists at $VENV_DIR — skipping creation."
else
    echo "Creating virtual environment at $VENV_DIR …"
    "$PYTHON" -m venv "$VENV_DIR"
    green "Virtual environment created."
fi

# Activate
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# Install dependencies
# ---------------------------------------------------------------------------

echo "Upgrading pip …"
pip install --quiet --upgrade pip

echo "Installing dependencies from requirements.txt …"
pip install --quiet -r requirements.txt

green "All dependencies installed."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
green "Setup complete!"
echo ""
echo "  Activate the environment:   source .venv/bin/activate"
echo "  Run first-time setup:       python mailrelay.py --setup"
echo "  Start with Debug Logs:      python mailrelay.py --debug"
echo "  Start MailRelay:            python mailrelay.py"
echo ""
