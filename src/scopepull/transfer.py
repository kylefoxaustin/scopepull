"""Streaming zip transfer with the Odyssey reliability contract.

The scope's export backend, especially for EnhancedVision observations, returns
an instant empty zip until the job is prepared (see docs/API.md). Reliable
pulling therefore means: cancel any stale job, keep the event pump actively
re-polling, wait an escalating warm-up, request the zip, and retry on an empty
or dropped transfer — at observation granularity, never re-pulling what landed.

Nothing here prints; it yields ProgressEvent objects the CLI renders.
"""

from __future__ import annotations

import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import Observation
from .client import PumpDead, ScopeClient

CHUNK = 256 * 1024
MAX_ATTEMPTS = 10
# A not-ready export = a ~10 KB End-Of-Central-Directory zip with no members.
EMPTY_ZIP_MAX = 20_000


class TransferError(Exception):
    """A pull that exhausted its retries."""


@dataclass
class ProgressEvent:
    obs_id: str
    attempt: int
    phase: str  # "warmup" | "downloading" | "empty" | "dropped" | "done" | "failed"
    bytes_done: int = 0
    frames_done: int = 0
    frames_total: int = 0
    detail: str = ""


def _warmup_seconds(attempt: int) -> float:
    """Escalating settle before requesting the zip. 9,12,15,... capped."""
    return min(6 + attempt * 3, 30)


def zip_has_frames(path: Path) -> bool:
    """True iff the zip is valid AND holds >=1 non-manifest member.

    Rejects both the instant empty zip and the manifest-only export.
    """
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False
            for name in z.namelist():
                if name.endswith("/") or name.endswith("manifest.json"):
                    continue
                return True
        return False
    except (zipfile.BadZipFile, OSError):
        return False


async def pull(
    client: ScopeClient,
    obs: Observation,
    dest_zip: Path,
    *,
    fmt: str = "tiff",
    max_attempts: int = MAX_ATTEMPTS,
    warmup: Callable[[int], float] = _warmup_seconds,
) -> AsyncIterator[ProgressEvent]:
    """Pull one observation's zip to dest_zip, retrying until it has real frames.

    Yields ProgressEvents. Raises TransferError if all attempts are exhausted.
    Writes to dest_zip.partial and renames on success, so dest_zip only ever
    exists as a complete, frame-bearing archive.
    """
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    partial = dest_zip.with_suffix(dest_zip.suffix + ".partial")
    url = f"/api/observations/zip/{fmt}/0x0/{obs.vpath}"

    for attempt in range(1, max_attempts + 1):
        # 1. Clear any stale server-side job (never raises on transport error).
        await client.cancel_download()

        # 2. Pump actively re-polling + escalating warm-up.
        settle = warmup(attempt)
        yield ProgressEvent(obs.obs_id, attempt, "warmup", detail=f"{settle:.0f}s")
        try:
            async with client.event_pump() as pump:
                await pump.wait_ready(timeout=settle + 15)
                # 3. Stream the zip.
                bytes_done = 0
                try:
                    async with client.stream_zip(url) as resp:
                        if resp.status_code == 502:
                            yield ProgressEvent(
                                obs.obs_id, attempt, "empty", detail="502 backend not ready"
                            )
                            continue
                        resp.raise_for_status()
                        with partial.open("wb") as f:
                            async for chunk in resp.aiter_bytes(CHUNK):
                                f.write(chunk)
                                bytes_done += len(chunk)
                                yield ProgressEvent(
                                    obs.obs_id,
                                    attempt,
                                    "downloading",
                                    bytes_done=bytes_done,
                                    frames_done=pump.progress.frames_done,
                                    frames_total=pump.progress.frames_total,
                                )
                except (httpx.TransportError, httpx.HTTPStatusError) as e:
                    partial.unlink(missing_ok=True)
                    yield ProgressEvent(obs.obs_id, attempt, "dropped", detail=type(e).__name__)
                    continue
        except PumpDead as e:
            yield ProgressEvent(obs.obs_id, attempt, "dropped", detail=str(e))
            continue

        # 4. Validate: real frames, not an empty/manifest-only export.
        if bytes_done < EMPTY_ZIP_MAX or not zip_has_frames(partial):
            partial.unlink(missing_ok=True)
            yield ProgressEvent(obs.obs_id, attempt, "empty", bytes_done=bytes_done)
            continue

        # 5. Commit: atomic rename to the final zip path.
        partial.replace(dest_zip)
        yield ProgressEvent(obs.obs_id, attempt, "done", bytes_done=bytes_done)
        return

    await client.cancel_download()
    raise TransferError(
        f"{obs.target}: export never returned frames after {max_attempts} attempts "
        "(scope busy or firmware export bug)"
    )
