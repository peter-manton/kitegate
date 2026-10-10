"""Append-only JSONL record of blocks, for an adult to review.

Used by the gateway. It records what the
child asked when a request was blocked, so the file is about a real child:
keep it out of git and backups you would not trust with that.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from .engine import Verdict

logger = logging.getLogger("kitegate.blocklog")


class BlockLog:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path or ""
        self._lock = threading.Lock()

    def record(self, user: Optional[str], direction: str, verdict: Verdict, asked: str) -> None:
        if not self.path:
            return
        try:
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "user": user or "unknown", "direction": direction,
                     "tier": verdict.tier, "rule": verdict.rule, "asked": asked}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # oversight must never break enforcement
            logger.exception("kitegate: could not write block log")
