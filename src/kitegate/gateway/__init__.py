"""The OpenAI-compatible Kitegate gateway. Needs the extra: pip install "kitegate[gateway]".

    kitegate            # reads its settings from the environment; see config.py
"""
from __future__ import annotations

import logging
import os

from ..packs import PackError
from .app import create_app
from .config import GatewaySettings, SettingsError

__all__ = ["GatewaySettings", "SettingsError", "create_app", "main"]


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = GatewaySettings.from_env()
    except SettingsError as exc:
        raise SystemExit(f"kitegate: {exc}") from None

    cert = os.environ.get("KITEGATE_TLS_CERT") or None
    key = os.environ.get("KITEGATE_TLS_KEY") or None
    if bool(cert) != bool(key):
        raise SystemExit("kitegate: set both KITEGATE_TLS_CERT and KITEGATE_TLS_KEY, or neither")
    host = os.environ.get("KITEGATE_HOST", "127.0.0.1")
    if not cert and host not in ("127.0.0.1", "::1", "localhost"):
        logging.getLogger("kitegate.gateway").warning(
            "serving plain HTTP on %s: keep this port on a private network (Docker network, "
            "or a TLS reverse proxy in front). Children's messages and the API key cross it.", host)
    try:
        app = create_app(settings)  # loads and signature-checks every pack
    except PackError as exc:
        raise SystemExit(f"kitegate: {exc}") from None
    uvicorn.run(app, host=host, port=int(os.environ.get("KITEGATE_PORT", "8080")),
                ssl_certfile=cert, ssl_keyfile=key, proxy_headers=False, server_header=False)
