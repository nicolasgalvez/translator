#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime_image="translator-runtime:${TRANSLATOR_SMOKE_TAG:-local}"
container=""

cleanup() {
    if [[ -n "$container" ]]; then
        docker rm --force "$container" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

cd "$repository_root"
docker build --platform linux/amd64 --target runtime --tag "$runtime_image" .

version="$(docker run --rm --platform linux/amd64 \
    --entrypoint /opt/translator/.venv/bin/python \
    "$runtime_image" --version)"
[[ "$version" == Python\ 3.11.* ]] || {
    echo "Expected Python 3.11 in the final environment, got: $version" >&2
    exit 1
}

stack="$(docker run --rm --platform linux/amd64 \
    --entrypoint /opt/translator/.venv/bin/python "$runtime_image" -c '
from importlib.metadata import version
import ctranslate2, faster_whisper, torch
assert torch.version.cuda == "12.4", torch.version.cuda
print(
    "torch={} torch-cuda={} ctranslate2={} faster-whisper={}".format(
        version("torch"), torch.version.cuda, ctranslate2.__version__,
        version("faster-whisper"),
    )
)
')"
echo "Final image stack: $stack"

container="$(docker run --detach --platform linux/amd64 --publish 127.0.0.1::8765 \
    --env TRANSLATOR_HOST=0.0.0.0 \
    --env TRANSLATOR_DEVICE='Smoke ALSA input' \
    --volume "$repository_root/tests/docker_smoke_app.py:/smoke/docker_smoke_app.py:ro" \
    "$runtime_image" /opt/translator/.venv/bin/python /smoke/docker_smoke_app.py)"
published="$(docker port "$container" 8765/tcp)"
url="http://${published}/"

for _attempt in {1..60}; do
    if response="$(curl --fail --silent --show-error "$url")"; then
        [[ "$response" == *"Live Transcriber"* ]] || {
            echo "Runtime returned HTTP 200 without the application page" >&2
            exit 1
        }
        echo "Docker runtime smoke passed at $url with $version."
        exit 0
    fi
    if ! docker inspect --format '{{.State.Running}}' "$container" | grep -qx true; then
        docker logs "$container" >&2
        exit 1
    fi
    sleep 1
done

docker logs "$container" >&2
echo "Docker runtime did not become reachable at $url" >&2
exit 1
