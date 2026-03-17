#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Defaults
PORT=8765
MODEL=small
DEVICE="BlackHole 2ch"

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  -p, --port PORT      Server port (default: 8765)"
    echo "  -m, --model MODEL    Whisper model: tiny, base, small, medium, large-v3 (default: small)"
    echo "  -d, --device DEVICE  Audio input device name (default: BlackHole 2ch)"
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
        -p|--port)   PORT="$2"; shift 2 ;;
        -m|--model)  MODEL="$2"; shift 2 ;;
        -d|--device) DEVICE="$2"; shift 2 ;;
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
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    echo "Setup complete."
else
    source "$VENV_DIR/bin/activate"
fi

export TRANSLATOR_PORT="$PORT"
export TRANSLATOR_MODEL="$MODEL"
export TRANSLATOR_DEVICE="$DEVICE"

echo "Starting translator (port=$PORT, model=$MODEL, device=$DEVICE)"
cd "$SCRIPT_DIR"
python app.py
