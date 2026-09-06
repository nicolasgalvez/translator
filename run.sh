#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

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

# Create venv and install deps on first run
if [ ! -d "$VENV_DIR" ]; then
    echo "First run — setting up virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    if [ "$BACKEND" = "mlx-whisper" ]; then
        pip install -r "$SCRIPT_DIR/requirements-mlx.txt" -q
    else
        pip install -r "$SCRIPT_DIR/requirements.txt" -q
    fi
    echo "Setup complete."
else
    source "$VENV_DIR/bin/activate"
    if [ "$BACKEND" = "mlx-whisper" ] && ! python -c "import mlx_whisper" 2>/dev/null; then
        echo "Installing mlx-whisper..."
        pip install mlx-whisper -q
    fi
fi

if [ -f "$SCRIPT_DIR/frontend/package.json" ]; then
    if [ ! -d "$SCRIPT_DIR/frontend/node_modules" ]; then
        echo "Installing frontend dependencies..."
        (cd "$SCRIPT_DIR/frontend" && npm install)
    fi

    if [ "$FRONTEND_DEV" = "1" ]; then
        echo "Starting frontend dev server at http://127.0.0.1:5173 ..."
        (cd "$SCRIPT_DIR/frontend" && npm run dev) &
        FRONTEND_PID=$!
        trap 'kill "$FRONTEND_PID" 2>/dev/null || true' EXIT
    elif [ "$SKIP_FRONTEND_BUILD" != "1" ]; then
        echo "Building frontend..."
        (cd "$SCRIPT_DIR/frontend" && npm run build)
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
python app.py
