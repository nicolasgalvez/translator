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

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

ENV TRANSLATOR_PORT=8765
ENV TRANSLATOR_MODEL=small
ENV TRANSLATOR_DEVICE="default"
ENV TRANSLATOR_LANGUAGE=es

EXPOSE ${TRANSLATOR_PORT}

CMD ["python3", "app.py"]
