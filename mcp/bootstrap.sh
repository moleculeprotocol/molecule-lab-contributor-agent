#!/usr/bin/env bash
# One-time setup for the mol-labs MCP server, run by the plugin's SessionStart hook.
#
# The human installs nothing. This script puts a private copy of uv into the plugin's
# persistent data directory, then asks uv to build the server's environment (a Python plus
# the packages declared at the top of server.py) ahead of time, so the first MCP spawn does
# not spend its startup timeout downloading. .mcp.json starts the server with that same uv.
#
# Runs on macOS and Linux under sh/bash, and on Windows under Git Bash, which the Claude
# desktop app's Code tab already requires. Idempotent: a second run with nothing to do prints
# nothing and exits 0. It never exits non-zero — a failed setup is reported in words for the
# agent to relay, not as a hook error the human has to decode.

set -u

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
DATA="${CLAUDE_PLUGIN_DATA:-}"
if [ -z "$ROOT" ] || [ -z "$DATA" ]; then
  echo "mol-labs setup skipped: CLAUDE_PLUGIN_ROOT or CLAUDE_PLUGIN_DATA is not set (not running as a plugin)."
  exit 0
fi

# Git Bash hands us Windows paths; make them usable here, and keep the Windows form for
# anything we hand to PowerShell.
WINDOWS=0
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) WINDOWS=1 ;; esac
if [ "$WINDOWS" = 1 ] && command -v cygpath >/dev/null 2>&1; then
  ROOT="$(cygpath -u "$ROOT")"
  DATA="$(cygpath -u "$DATA")"
fi

SERVER="$ROOT/mcp/server.py"
UV_DIR="$DATA/uv"
UV_BIN="$UV_DIR/uv"
[ "$WINDOWS" = 1 ] && UV_BIN="$UV_DIR/uv.exe"
LOG="$DATA/bootstrap.log"
STAMP="$DATA/server.stamp"
LOCK="$DATA/bootstrap.lock"

# Everything uv downloads lives under the plugin's own data directory: the interpreter, the
# package cache, the script environment. Uninstalling the plugin's data removes all of it,
# and nothing here touches a uv the human may already use.
export UV_CACHE_DIR="$DATA/cache"
export UV_PYTHON_INSTALL_DIR="$DATA/python"
export UV_PYTHON_PREFERENCE=only-managed   # never depend on whatever Python the machine has
export UV_NO_PROGRESS=1

mkdir -p "$DATA" "$UV_DIR" 2>/dev/null

# The MCP server is spawned by the host at the same time this hook runs. Both a second
# session and that spawn must simply wait for, or skip, an in-progress setup.
if ! mkdir "$LOCK" 2>/dev/null; then
  # A lock older than ten minutes is a crash, not a run in progress.
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "=== $(date) ==="
  echo "root=$ROOT data=$DATA windows=$WINDOWS"
} >>"$LOG" 2>&1

fail() {
  echo "mol-labs setup did not finish: $1"
  echo "Details are in $LOG. The mol-labs tools will not be available until this is fixed."
  echo "Manual fallback: install uv from https://docs.astral.sh/uv/ so it is on PATH, then start a new session; setup will pick it up from there."
  exit 0
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else cat "$1" | wc -c
  fi
}

did_something=0

# --- 1. a private uv ------------------------------------------------------------------

if ! "$UV_BIN" --version >>"$LOG" 2>&1; then
  did_something=1
  existing="$(command -v uv 2>/dev/null || true)"
  if [ -n "$existing" ] && "$existing" --version >>"$LOG" 2>&1; then
    echo "copying existing uv from $existing" >>"$LOG"
    cp "$existing" "$UV_BIN" 2>>"$LOG" || fail "could not copy the uv already installed at $existing."
  elif [ "$WINDOWS" = 1 ]; then
    win_dir="$(cygpath -w "$UV_DIR" 2>/dev/null || echo "$UV_DIR")"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
      "\$env:UV_INSTALL_DIR='$win_dir'; \$env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex" \
      >>"$LOG" 2>&1 || fail "the uv installer failed (is the machine online?)."
  else
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh 2>>"$LOG" \
        | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh >>"$LOG" 2>&1 \
        || fail "the uv installer failed (is the machine online?)."
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh 2>>"$LOG" \
        | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh >>"$LOG" 2>&1 \
        || fail "the uv installer failed (is the machine online?)."
    else
      fail "neither curl nor wget is available to download uv."
    fi
  fi
  "$UV_BIN" --version >>"$LOG" 2>&1 || fail "uv was installed to $UV_DIR but does not run."
fi

# --- 2. the server's environment, built ahead of the first spawn -----------------------

want="$(sha256_of "$SERVER")"
have="$(cat "$STAMP" 2>/dev/null || true)"
if [ "$want" != "$have" ]; then
  did_something=1
  echo "syncing script environment for $SERVER" >>"$LOG"
  if "$UV_BIN" sync --script "$SERVER" >>"$LOG" 2>&1; then
    printf '%s\n' "$want" >"$STAMP"
  else
    fail "uv could not build the server's Python environment (is the machine online?)."
  fi
fi

# --- 3. let the host retry the server now, not in fifteen minutes ----------------------
#
# If this first-time setup took longer than the host's startup timeout, the server spawned
# alongside it has already been marked failed, and Claude Code remembers a failed plugin
# server for fifteen minutes in this cache file before it will try again. Dropping our own
# entry means /reload-plugins works immediately. Best effort: if the file or its shape ever
# changes, the only cost is that fifteen-minute wait.

if [ "$did_something" = 1 ]; then
  cache="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-needs-auth-cache.json"
  [ "$WINDOWS" = 1 ] && [ -n "${USERPROFILE:-}" ] && cache="${CLAUDE_CONFIG_DIR:-$(cygpath -u "$USERPROFILE")/.claude}/mcp-needs-auth-cache.json"
  if [ -f "$cache" ]; then
    "$UV_BIN" run --no-project --python 3 python - "$cache" >>"$LOG" 2>&1 <<'PY' || true
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
if isinstance(data, dict):
    dropped = [k for k in data if k.endswith(":mol-labs")]
    for k in dropped:
        del data[k]
    if dropped:
        with open(path, "w") as f:
            json.dump(data, f)
        print("cleared cached failure for", dropped)
PY
  fi
fi

if [ "$did_something" = 1 ]; then
  echo "mol-labs: first-time setup finished. A private uv and the server's Python environment are installed under $DATA; the human installed nothing."
  echo "If the mol-labs MCP tools are not connected in this session, ask the human to type /reload-plugins once — the server was spawned before setup finished."
fi
exit 0
