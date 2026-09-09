FROM node:26-bookworm-slim AS frontend

WORKDIR /frontend
COPY frontend/package*.json frontend/.npmrc ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04 AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libportaudio2 libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
ENV UV_PROJECT_ENVIRONMENT=/opt/translator/.venv

# Dependencies before source, so a code change does not re-resolve or re-download
# several GB of CUDA torch.
#
# --extra cuda, not cpu: both CI and this image are Linux, so sys_platform cannot
# tell them apart. The extra is what picks the CUDA index.
COPY pyproject.toml uv.lock ./
RUN uv python install 3.11 \
    && uv sync --python 3.11 --locked --no-dev --extra cuda \
    && /opt/translator/.venv/bin/python -c \
        'import sys; assert sys.version_info[:2] == (3, 11), sys.version'

COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

ENV TRANSLATOR_HOST=0.0.0.0
ENV TRANSLATOR_PORT=8765
ENV TRANSLATOR_MODEL=small
ENV TRANSLATOR_LANGUAGE=es

EXPOSE ${TRANSLATOR_PORT}

FROM runtime-base AS runtime
CMD ["/opt/translator/.venv/bin/python", "app.py"]
