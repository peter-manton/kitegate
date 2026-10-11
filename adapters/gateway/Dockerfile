# Kitegate gateway image. Build from the repo root:
#   docker build -f adapters/gateway/Dockerfile -t kitegate .
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 app
WORKDIR /srv

COPY pyproject.toml README.md LICENSE ./
# Every dependency, direct and indirect, at an exact version with its hashes, so a
# build installs what was reviewed and a swapped package fails. To refresh it:
#   uv pip compile pyproject.toml --extra gateway --universal --python-version 3.14 \
#     --generate-hashes --no-header -o requirements/gateway.txt
COPY requirements/gateway.txt ./requirements/gateway.txt
COPY src ./src
RUN pip install --require-hashes -r requirements/gateway.txt \
 && pip install --no-deps . \
 && mkdir -p /var/lib/kitegate && chown app /var/lib/kitegate

USER app
# 0.0.0.0 inside the container so other containers on the Compose network can
# reach it. Do not publish this port on a host interface unless TLS is on
# (KITEGATE_TLS_CERT/KEY) or a TLS reverse proxy sits in front.
ENV KITEGATE_HOST=0.0.0.0 KITEGATE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["kitegate"]
