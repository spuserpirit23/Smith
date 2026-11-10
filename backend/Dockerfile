# Multi-arch (amd64 / arm64). On AWS, arm64 Graviton (t4g) is the cheapest fit.
FROM python:3.12-slim AS base

# EXTRAS: comma list of optional dependency groups (aws, wake, local, speech, opus).
# The default is the zero-API-cost stack: Piper TTS on the box + free Google Web Speech STT.
ARG EXTRAS=local,speech
# Piper voices baked into the image when EXTRAS includes "local" (rhasspy/piper-voices paths).
ARG PIPER_VOICES="en/en_US/lessac/medium/en_US-lessac-medium hi/hi_IN/pratham/medium/hi_IN-pratham-medium"
ARG PIPER_VOICES_URL=https://huggingface.co/rhasspy/piper-voices/resolve/main
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && if echo "$EXTRAS" | grep -q opus; then apt-get install -y --no-install-recommends libopus0; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY mitra ./mitra
RUN pip install ".[${EXTRAS}]" \
    && if echo "$EXTRAS" | grep -q wake; then \
         pip install --no-deps openwakeword==0.6.0 \
         && python -c "from openwakeword.utils import download_models; download_models(model_names=['hey_jarvis'])"; \
       fi \
    && if echo "$EXTRAS" | grep -q local; then \
         mkdir -p /models/piper && for v in $PIPER_VOICES; do \
           for ext in onnx onnx.json; do \
             curl -fsSL --retry 3 -o "/models/piper/$(basename $v).$ext" "$PIPER_VOICES_URL/$v.$ext"; \
           done; \
         done; \
       fi
COPY config ./config

RUN useradd --uid 10001 --create-home mitra && mkdir -p /data /mosquitto/auth && chown -R mitra /data /mosquitto/auth
USER mitra

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD curl -fsS http://localhost:8080/healthz || exit 1
CMD ["uvicorn", "mitra.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--ws-max-size", "1048576", "--proxy-headers", "--forwarded-allow-ips", "*"]
