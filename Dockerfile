FROM node:26-bookworm-slim AS frontend

WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nvidia/cuda:13.3.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    libportaudio2 libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source, so a code change does not re-resolve or re-download
# several GB of CUDA torch.
#
# --extra cuda, not cpu: both CI and this image are Linux, so sys_platform cannot
# tell them apart. The extra is what picks the CUDA index.
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/usr/local
RUN uv sync --locked --no-dev --extra cuda

COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

ENV TRANSLATOR_PORT=8765
ENV TRANSLATOR_MODEL=small
ENV TRANSLATOR_DEVICE="default"
ENV TRANSLATOR_LANGUAGE=es

EXPOSE ${TRANSLATOR_PORT}

CMD ["python3", "app.py"]
