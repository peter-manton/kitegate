"""Append-only JSONL record of blocks, for an adult to review.

Used by the gateway. It records what the
child asked when a request was blocked, so the file is about a real child:
keep it out of git and backups you would not trust with that. It is created
readable by its owner only (mode 0600, in a directory made 0700); on Windows
those modes are not enforced, so restrict the folder there.
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

_OWNER_ONLY_FILE = 0o600
_OWNER_ONLY_DIR = 0o700


class BlockLog:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path or ""
        self._lock = threading.Lock()

    def _open(self):
        """The log, opened for appending and never group- or world-readable."""
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, mode=_OWNER_ONLY_DIR, exist_ok=True)
        # The mode applies when the file is created, so there is no window in
        # which it exists with broader permissions than its owner's.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _OWNER_ONLY_FILE)
        try:
            if os.name == "posix" and os.fstat(fd).st_mode & 0o077:
                # A log from an earlier version, or one made by hand.
                os.fchmod(fd, _OWNER_ONLY_FILE)
        except OSError:
            logger.warning("kitegate: could not restrict %s to its owner", self.path)
        return os.fdopen(fd, "a", encoding="utf-8")

    def record(self, user: Optional[str], direction: str, verdict: Verdict, asked: str) -> None:
        if not self.path:
            return
        try:
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "user": user or "unknown", "direction": direction,
                     "tier": verdict.tier, "rule": verdict.rule, "asked": asked}
            with self._lock, self._open() as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # oversight must never break enforcement
            logger.exception("kitegate: could not write block log")
