# Kitegate gateway image. Build from the repo root:
#   docker build -f adapters/gateway/Dockerfile -t kitegate .
#
# Two stages. The first installs the hash-locked dependencies and the package
# into a plain directory; the second is distroless: Python and nothing else.
# No shell, no package manager, no pip, no setuptools, so there is nothing in
# the final image to run a command with, and nothing for a scanner to flag
# that the gateway does not use. The user is numeric and not root.
#
# The two stages must share a Python minor version (3.13): the wheels are
# built for the build stage's interpreter and run on the runtime's. Bump both
# together, and regenerate the lock below with --python-version to match.

# -- build ---------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

# Every dependency, direct and indirect, at an exact version with its hashes, so a
# build installs what was reviewed and a swapped package fails. To refresh it:
#   uv pip compile pyproject.toml --extra gateway --universal --python-version 3.13 \
#     --generate-hashes --no-header -o requirements/gateway.txt
# --only-binary: never run a source build (setup.py) while building this image.
COPY requirements/gateway.txt ./requirements/gateway.txt
RUN pip install --require-hashes --only-binary=:all: --target /site -r requirements/gateway.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# /site/bin holds console scripts whose shebang points at this stage's Python,
# which does not exist in the runtime stage; the image runs the module instead.
RUN pip install --no-deps --no-compile --target /site . \
 && rm -rf /site/bin \
 && mkdir -p /var/lib/kitegate

# -- run -----------------------------------------------------------------------
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:83aa8d4f74a4d7f7cf2d472054139bef71a927b76c680c0f2e1021d6b1d6d732

LABEL org.opencontainers.image.title="kitegate gateway" \
      org.opencontainers.image.description="OpenAI-compatible gateway that screens children's chats" \
      org.opencontainers.image.source="https://github.com/peter-manton/kitegate" \
      org.opencontainers.image.licenses="Apache-2.0"

# PYTHONSAFEPATH: the working directory is not put on sys.path.
ENV PYTHONPATH=/site \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    KITEGATE_HOST=0.0.0.0 \
    KITEGATE_PORT=8080

COPY --from=build /site /site
# Where the block log goes. A named volume mounted here starts with this owner.
COPY --from=build --chown=10001:10001 /var/lib/kitegate /var/lib/kitegate

# Numeric, so a runtime that cannot resolve names (Kubernetes runAsNonRoot) can
# still verify it is not root; distroless needs no passwd entry for it. 10001 is
# the uid earlier images ran as, so a block-log volume from an existing install
# is still writable. Changing it would stop block logging on upgrade, quietly.
USER 10001:10001
WORKDIR /var/lib/kitegate

# 0.0.0.0 inside the container so other containers on the Compose network can
# reach it. Do not publish this port on a host interface unless TLS is on
# (KITEGATE_TLS_CERT/KEY) or a TLS reverse proxy sits in front.
EXPOSE 8080

# Exec form: there is no shell to run a shell-form command with.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD ["/usr/bin/python3.13", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["/usr/bin/python3.13", "-m", "kitegate.gateway"]
