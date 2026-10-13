# NeMo Guardrails server with the Kitegate content-safety config.
#   docker build -f adapters/nemo/Dockerfile -t kitegate-nemo adapters/nemo
# nemoguardrails supports Python 3.10 to 3.13.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEMO_GUARDRAILS_NO_USAGE_STATS=1
RUN useradd --create-home --uid 10001 app
RUN pip install "nemoguardrails[server]==0.24.1"

COPY config /config
USER app
EXPOSE 8000
# Only the Kitegate gateway should reach this: the NeMo server has no API auth.
CMD ["nemoguardrails", "server", "--config", "/config", "--default-config-id", "kitegate", \
     "--port", "8000", "--disable-chat-ui"]
