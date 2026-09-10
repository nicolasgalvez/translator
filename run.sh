#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defaults
HOST=127.0.0.1
PORT=8765
MODEL=small
DEVICE="BlackHole 2ch"
BACKEND=faster-whisper
LANGUAGE=es
FRONTEND_DEV=0
SKIP_FRONTEND_BUILD=0

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  -H, --host HOST      Bind address (default: 127.0.0.1)"
    echo "  -p, --port PORT      Server port (default: 8765)"
    echo "  -m, --model MODEL    Whisper model: tiny, base, small, medium, large-v3 (default: small)"
    echo "  -d, --device DEVICE  Audio input device name (default: BlackHole 2ch)"
    echo "  -b, --backend NAME   Transcription backend: faster-whisper, mlx-whisper (default: faster-whisper)"
    echo "  -l, --language CODE  Spoken language, e.g. en, es, ja, or 'auto' to detect (default: es)"
    echo "      --frontend-dev   Start the Vite dev server on http://127.0.0.1:5173"
    echo "      --skip-frontend-build"
    echo "                       Do not build the React frontend before starting"
    echo "  -h, --help           Show this help"
    echo ""
    echo "Models (speed vs accuracy):"
    echo "  tiny     — fastest, least accurate (~1GB RAM)"
    echo "  base     — fast, decent accuracy (~1GB RAM)"
    echo "  small    — good balance (default) (~2GB RAM)"
    echo "  medium   — slower, better accuracy (~5GB RAM)"
    echo "  large-v3 — slowest, best accuracy (~10GB RAM)"
    echo ""
    echo "CUDA is auto-detected. If a GPU is available, it will be used."
    echo ""
    echo "Examples:"
    echo "  $0                              # defaults"
    echo "  $0 --model large-v3             # best accuracy (needs GPU for real-time)"
    echo "  $0 --port 9000 --model medium"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        -H|--host)   HOST="$2"; shift 2 ;;
        -p|--port)   PORT="$2"; shift 2 ;;
        -m|--model)  MODEL="$2"; shift 2 ;;
        -d|--device) DEVICE="$2"; shift 2 ;;
        -b|--backend) BACKEND="$2"; shift 2 ;;
        -l|--language) LANGUAGE="$2"; shift 2 ;;
        --frontend-dev) FRONTEND_DEV=1; shift ;;
        --skip-frontend-build) SKIP_FRONTEND_BUILD=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# uv creates and syncs .venv from uv.lock on demand, so there is no first-run
# branch, no manual activation, and no "is mlx_whisper importable yet" probe —
# switching backends just changes which extra is synced.
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required but not installed." >&2
    echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "See https://docs.astral.sh/uv/ for other options." >&2
    exit 1
fi

SYNC_ARGS=(--quiet)
if [ "$BACKEND" = "mlx-whisper" ]; then
    SYNC_ARGS+=(--extra mlx)
fi
(cd "$SCRIPT_DIR" && uv sync "${SYNC_ARGS[@]}")

# True when any build input is newer than the bundle, or there is no bundle.
# dist/index.html is the reference because vite always emits it, so its mtime
# marks when the bundle was last produced.
#
# Errs toward building: a needless rebuild costs seconds, while a skipped one
# serves a stale bundle that looks like a code change silently not working.
frontend_needs_build() {
    local fe="$SCRIPT_DIR/frontend"
    local ref="$fe/dist/index.html"

    [ -f "$ref" ] || return 0

    local inputs=("$fe/src" "$fe/index.html" "$fe/package.json" "$fe/package-lock.json")
    local cfg
    for cfg in "$fe"/vite.config.*; do
        [ -e "$cfg" ] && inputs+=("$cfg")
    done

    [ -n "$(find "${inputs[@]}" -newer "$ref" -print -quit 2>/dev/null)" ]
}

if [ -f "$SCRIPT_DIR/frontend/package.json" ]; then
    # npm writes node_modules/.package-lock.json on install, so a lock file
    # newer than it means the tree is out of date. Checking only that
    # node_modules exists — as this did — never reinstalls after a lock change.
    if [ ! -d "$SCRIPT_DIR/frontend/node_modules" ] || \
       [ "$SCRIPT_DIR/frontend/package-lock.json" -nt "$SCRIPT_DIR/frontend/node_modules/.package-lock.json" ]; then
        echo "Installing frontend dependencies..."
        (cd "$SCRIPT_DIR/frontend" && npm install)
    fi

    if [ "$FRONTEND_DEV" = "1" ]; then
        echo "Starting frontend dev server at http://127.0.0.1:5173 ..."
        (cd "$SCRIPT_DIR/frontend" && npm run dev) &
        FRONTEND_PID=$!
        trap 'kill "$FRONTEND_PID" 2>/dev/null || true' EXIT
    elif [ "$SKIP_FRONTEND_BUILD" = "1" ]; then
        echo "Skipping frontend build (--skip-frontend-build)."
    elif frontend_needs_build; then
        echo "Building frontend..."
        (cd "$SCRIPT_DIR/frontend" && npm run build)
    else
        # Say so out loud: a silent skip is indistinguishable from a staleness bug.
        echo "Frontend up to date - skipping build."
    fi
fi

export TRANSLATOR_HOST="$HOST"
export TRANSLATOR_PORT="$PORT"
export TRANSLATOR_MODEL="$MODEL"
export TRANSLATOR_DEVICE="$DEVICE"
export TRANSLATOR_BACKEND="$BACKEND"
export TRANSLATOR_LANGUAGE="$LANGUAGE"

echo "Starting transcriber (host=$HOST, port=$PORT, model=$MODEL, device=$DEVICE, backend=$BACKEND, language=$LANGUAGE)"
cd "$SCRIPT_DIR"
uv run python app.py
