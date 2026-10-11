# NeMo Guardrails server with the Kitegate content-safety config.
#   docker build -f adapters/nemo/Dockerfile -t kitegate-nemo adapters/nemo
# nemoguardrails supports Python 3.10 to 3.13.
FROM python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

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
