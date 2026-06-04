#!/usr/bin/env bash
# Wine + MT5 + MetaTrader5-Python bootstrap for Ubuntu 24.04.
#
# What this DOES (automated, no interaction):
#   - Installs Wine and winetricks via apt (needs your sudo password ONCE)
#   - Creates an isolated WINEPREFIX at ~/.gold-mt5-wine
#   - Installs the Windows libraries MT5 needs (vcrun, dotnet, fonts)
#   - Downloads the MT5 installer
#   - Downloads embeddable Windows Python 3.11 and unpacks it inside the prefix
#   - Installs the MetaTrader5 pip package into that Windows Python
#   - Generates start-bridge.sh you can run from cron / systemd
#
# What this does NOT do (requires you):
#   - Running the MT5 installer (it's a GUI clickthrough)
#   - Logging into your broker inside MT5 (your credentials)
#   - Adding the GOLD symbol to Market Watch
#   - Setting GOLD_BRIDGE_SECRET, MT5_LOGIN/PASSWORD/SERVER env vars
#
# After this script finishes, follow docs/HANDBOOK.md §12.
set -euo pipefail

PREFIX="${GOLD_MT5_PREFIX:-$HOME/.gold-mt5-wine}"
PY_VER="${GOLD_MT5_PYVER:-3.11.9}"
ARCH="win64"

log() { printf '\033[1;36m[bootstrap] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[bootstrap] %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname)" == "Linux" ]] || die "Linux only"

# ---------------------------------------------------------------------------
# 1. Wine + winetricks
# ---------------------------------------------------------------------------
if ! command -v wine >/dev/null 2>&1; then
    log "Installing Wine and winetricks (will prompt for sudo)..."
    sudo dpkg --add-architecture i386
    sudo apt update
    sudo apt install -y wine winetricks cabextract wget xvfb
else
    log "Wine already installed: $(wine --version)"
fi

if ! command -v winetricks >/dev/null 2>&1; then
    warn "winetricks not on PATH — checking ~/.local/bin"
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v winetricks >/dev/null 2>&1; then
    warn "winetricks unavailable; skipping Windows-runtime install."
    warn "If MT5 install or pip install fails, install manually:"
    warn "  sudo apt install -y winetricks cabextract"
    warn "  winetricks -q corefonts vcrun2019"
    SKIP_WINETRICKS=1
fi

# ---------------------------------------------------------------------------
# 2. Isolated WINEPREFIX (so MT5 doesn't pollute ~/.wine)
# ---------------------------------------------------------------------------
export WINEPREFIX="$PREFIX"
export WINEARCH=win64
export WINEDEBUG=-all

if [[ ! -d "$WINEPREFIX/drive_c" ]]; then
    log "Creating WINEPREFIX at $WINEPREFIX"
    mkdir -p "$WINEPREFIX"
    wineboot --init
    wineserver -w
else
    log "WINEPREFIX already exists at $WINEPREFIX"
fi

# ---------------------------------------------------------------------------
# 3. Windows libs MT5 needs
# ---------------------------------------------------------------------------
if [[ "${SKIP_WINETRICKS:-0}" == "1" ]]; then
    log "Skipping winetricks step (not installed)"
else
    WINETRICKS_FLAGS=("-q")
    WANTED_VERBS=(corefonts vcrun2019)
    for v in "${WANTED_VERBS[@]}"; do
        if [[ ! -f "$WINEPREFIX/.winetricks-$v.done" ]]; then
            log "winetricks: installing $v (this can take a few minutes)"
            winetricks "${WINETRICKS_FLAGS[@]}" "$v" || warn "winetricks $v reported a non-zero exit (often harmless)"
            touch "$WINEPREFIX/.winetricks-$v.done"
        else
            log "winetricks $v already done"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 4. MT5 installer download (you click through it manually next)
# ---------------------------------------------------------------------------
INSTALLER="$WINEPREFIX/mt5setup.exe"
if [[ ! -f "$INSTALLER" ]]; then
    log "Downloading MT5 installer"
    wget -q --show-progress -O "$INSTALLER" "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
fi
log "MT5 installer staged at $INSTALLER"

# ---------------------------------------------------------------------------
# 5. Windows-Python embeddable inside the prefix
# ---------------------------------------------------------------------------
WINPY_DIR="$WINEPREFIX/drive_c/winpy"
WINPY_EXE="$WINPY_DIR/python.exe"
if [[ ! -x "$WINPY_EXE" ]]; then
    log "Downloading Windows Python $PY_VER embeddable"
    mkdir -p "$WINPY_DIR"
    TMPZIP="$(mktemp --suffix=.zip)"
    wget -q --show-progress -O "$TMPZIP" "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"
    unzip -q -o "$TMPZIP" -d "$WINPY_DIR"
    rm -f "$TMPZIP"
    # Enable site-packages in embeddable distro.
    PTH_FILE="$(find "$WINPY_DIR" -maxdepth 1 -name 'python*._pth' | head -1)"
    if [[ -n "$PTH_FILE" ]]; then
        sed -i 's/^#import site/import site/' "$PTH_FILE"
    fi    # Bootstrap pip.
    log "Bootstrapping pip in Windows-Python"
    GET_PIP="$WINPY_DIR/get-pip.py"
    wget -q -O "$GET_PIP" https://bootstrap.pypa.io/get-pip.py
    wine "$WINPY_EXE" "$(winepath -w "$GET_PIP")" --no-warn-script-location
fi

# ---------------------------------------------------------------------------
# 6. MetaTrader5 + the bridge's deps (stdlib-only — none beyond MT5).
# ---------------------------------------------------------------------------
log "Installing MetaTrader5 pip package into Windows-Python"
# Pin numpy<2 — Wine 9.x ucrtbase lacks crealf used by numpy 2.x.
wine "$WINPY_EXE" -m pip install --no-warn-script-location --upgrade pip "numpy<2" MetaTrader5 || \
    warn "MetaTrader5 install failed — will retry after MT5 GUI install"

# Ensure the repo's `src` directory is importable from the embeddable Python
# (PYTHONPATH is ignored when a python*._pth file is present, so we append
# the winepath of <repo>/src into that file).
PTH_FILE="$(find "$WINPY_DIR" -maxdepth 1 -name 'python*._pth' | head -1)"
REPO_DIR_FOR_PTH="$(cd "$(dirname "$0")/.." && pwd)"
SRC_WIN="$(winepath -w "$REPO_DIR_FOR_PTH/src")"
if [[ -n "$PTH_FILE" ]] && ! grep -qF "$SRC_WIN" "$PTH_FILE"; then
    log "Adding repo src to $PTH_FILE"
    printf '%s\n' "$SRC_WIN" >> "$PTH_FILE"
fi

# ---------------------------------------------------------------------------
# 7. Stage the bridge runner
# ---------------------------------------------------------------------------
RUNNER="$PREFIX/start-bridge.sh"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cat > "$RUNNER" <<EOF
#!/usr/bin/env bash
# Auto-generated by setup_wine_mt5.sh — runs the gold-trader bridge under Wine.
set -euo pipefail
export WINEPREFIX="$PREFIX"
export WINEARCH=win64
export WINEDEBUG=-all
export PYTHONPATH="\$(winepath -w "$REPO_DIR/src")"

# Required env vars (set these before running, or in a wrapper):
: "\${GOLD_BRIDGE_SECRET:?set GOLD_BRIDGE_SECRET to a long random string}"
: "\${MT5_LOGIN:?set MT5_LOGIN to your broker account number}"
: "\${MT5_PASSWORD:?set MT5_PASSWORD to your investor or trading password}"
: "\${MT5_SERVER:?set MT5_SERVER to your broker MT5 server name}"

export GOLD_SYMBOL="\${GOLD_SYMBOL:-GOLD}"
export GOLD_MAGIC="\${GOLD_MAGIC:-20260507}"
export GOLD_BRIDGE_HOST="\${GOLD_BRIDGE_HOST:-127.0.0.1}"
export GOLD_BRIDGE_PORT="\${GOLD_BRIDGE_PORT:-8765}"
export MT5_ACCOUNT_TYPE="\${MT5_ACCOUNT_TYPE:-demo}"

cd "$REPO_DIR"
exec wine "$WINPY_DIR/python.exe" -m gold_trader.live.mt5_bridge_server \\
    --host "\$GOLD_BRIDGE_HOST" --port "\$GOLD_BRIDGE_PORT" \\
    --symbol "\$GOLD_SYMBOL" --magic "\$GOLD_MAGIC"
EOF
chmod +x "$RUNNER"

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
cat <<EOF

\033[1;32m======================================================================
 Bootstrap complete.
======================================================================\033[0m

 WINEPREFIX:        $WINEPREFIX
 Windows Python:    $WINPY_EXE
 MT5 installer:     $INSTALLER
 Bridge runner:     $RUNNER

 NEXT (manual, see docs/HANDBOOK.md §12):
   1. Run the MT5 installer:
        WINEPREFIX="$WINEPREFIX" wine "$INSTALLER"
   2. In MT5: log into your broker (demo first), add GOLD to Market Watch.
   3. Set env vars (GOLD_BRIDGE_SECRET, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER).
   4. Start the bridge:  $RUNNER
   5. From this Linux shell, in another terminal:
        export GOLD_BROKER=mt5_remote
        export GOLD_BRIDGE_SECRET=<same secret>
        .venv/bin/python -m gold_trader.cli broker-info

EOF
