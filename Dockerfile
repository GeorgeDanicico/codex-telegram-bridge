# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim

ARG CODEX_VERSION=latest

RUN apt-get update \
    && apt-get install --no-install-recommends --yes python3 ca-certificates \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin bridge \
    && mkdir /workspace \
    && chown bridge:bridge /workspace

WORKDIR /app
COPY --chown=bridge:bridge bot.py ./bot.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bridge

USER bridge
ENTRYPOINT ["python3", "/app/bot.py"]
