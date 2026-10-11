"""The block log holds what children typed, so it is private to its owner."""
import json
import os
import stat

import pytest

from kitegate.blocklog import BlockLog
from kitegate.engine import Verdict

BLOCK = Verdict(False, "hard_block", "adult.explicit", "no")
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are not enforced on Windows")


def test_a_block_is_appended_as_one_json_line(tmp_path):
    path = tmp_path / "logs" / "blocks.jsonl"
    log = BlockLog(str(path))
    log.record("child@example.org", "request", BLOCK, "first")
    log.record(None, "response", BLOCK, "second")
    first, second = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert first["user"] == "child@example.org" and first["asked"] == "first"
    assert second["user"] == "unknown" and second["rule"] == "adult.explicit"


def test_no_path_means_no_log(tmp_path):
    BlockLog("").record("child@example.org", "request", BLOCK, "x")
    assert list(tmp_path.iterdir()) == []


def test_a_write_failure_never_raises(tmp_path):
    # A directory where the file should be: opening it fails, enforcement does not.
    BlockLog(str(tmp_path)).record("child@example.org", "request", BLOCK, "x")


@posix_only
def test_the_log_and_its_new_folder_are_owner_only(tmp_path):
    path = tmp_path / "logs" / "blocks.jsonl"
    BlockLog(str(path)).record("child@example.org", "request", BLOCK, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


@posix_only
def test_an_older_world_readable_log_is_tightened(tmp_path):
    path = tmp_path / "blocks.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    BlockLog(str(path)).record("child@example.org", "request", BLOCK, "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
