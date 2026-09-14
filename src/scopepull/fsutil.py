"""Filesystem helpers that survive Windows.

On Windows, a file you just finished writing is often still held open for a
second or two by Windows Defender's real-time scan (or by an indexer, or by a
backup agent). os.replace() and os.unlink() then fail with
PermissionError / WinError 32 "being used by another process" -- which is
what killed a fully downloaded, fully validated 200 MB NGC 891 export at the
very last step. The fix is not to be clever; it is to try again.
"""

from __future__ import annotations

import time
from pathlib import Path

# ~30 s total: 0.2 + 0.4 + 0.8 + ... capped at 5 s per wait.
_DELAYS = [min(0.2 * 2**i, 5.0) for i in range(12)]


def replace_retry(src: Path, dst: Path, delays: list[float] | None = None) -> None:
    """os.replace(src, dst), retried while Windows says the file is busy."""
    last: BaseException | None = None
    for delay in [0.0, *(delays if delays is not None else _DELAYS)]:
        if delay:
            time.sleep(delay)
        try:
            Path(src).replace(dst)
            return
        except PermissionError as e:  # WinError 32 / 5 while a scanner holds the file
            last = e
    assert last is not None
    raise last


def unlink_retry(path: Path, delays: list[float] | None = None) -> None:
    """path.unlink(missing_ok=True), retried while Windows says it is busy."""
    last: BaseException | None = None
    for delay in [0.0, *(delays if delays is not None else _DELAYS)]:
        if delay:
            time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as e:
            last = e
    assert last is not None
    raise last
