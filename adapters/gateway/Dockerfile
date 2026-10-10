# Kitegate gateway image. Build from the repo root:
#   docker build -f adapters/gateway/Dockerfile -t kitegate .
FROM python:3.14.8-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 app
WORKDIR /srv

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[gateway]" && mkdir -p /var/lib/kitegate && chown app /var/lib/kitegate

USER app
# 0.0.0.0 inside the container so other containers on the Compose network can
# reach it. Do not publish this port on a host interface unless TLS is on
# (KITEGATE_TLS_CERT/KEY) or a TLS reverse proxy sits in front.
ENV KITEGATE_HOST=0.0.0.0 KITEGATE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["kitegate"]
