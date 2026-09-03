"""Streaming zip transfer using the scope's real export protocol.

Cracked from browser HAR captures (docs/API.md): the scope builds the zip
frame-by-frame on the server (~1 frame/sec) and reports progress over the
/api/event long-poll as status "started" with progress climbing 1..nb_frames,
then a terminal status "ended" when the archive is ready to download.

Correct sequence (NOT the old cancel-and-retry approach, which aborted the
build every 30s):
    1. cancelDownload ONCE to clear any stale prior job
    2. trigger the build (GET the zip; returns an instant empty zip while it
       builds — that's expected)
    3. keep ONE event pump polling; wait for "started", then "ended". NEVER
       cancel during the build.
    4. GET the zip again -> now it streams the real archive
    5. validate frames, atomic rename

Nothing here prints; it yields ProgressEvent objects the CLI renders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import Observation
from .client import PumpDead, ScopeClient

CHUNK = 256 * 1024
# A not-ready export = a ~10 KB End-Of-Central-Directory zip with no members.
EMPTY_ZIP_MAX = 20_000
# Build time ~1 frame/sec; give generous headroom, floor and cap in seconds.
PER_FRAME_BUDGET = 4.0
MIN_BUILD_TIMEOUT = 180.0
MAX_BUILD_TIMEOUT = 45 * 60.0
STARTED_TIMEOUT = 90.0


class TransferError(Exception):
    """A pull that could not complete."""


@dataclass
class ProgressEvent:
    obs_id: str
    phase: str  # "trigger"|"building"|"downloading"|"done"|"failed"
    bytes_done: int = 0
    frames_done: int = 0
    frames_total: int = 0
    detail: str = ""


def _build_timeout(nb_frames: int) -> float:
    return min(MAX_BUILD_TIMEOUT, max(MIN_BUILD_TIMEOUT, nb_frames * PER_FRAME_BUDGET))


def zip_has_frames(path: Path) -> bool:
    """True iff the zip is valid AND holds >=1 non-manifest member."""
    import zipfile

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


async def _stream_to(client: ScopeClient, url: str, dest: Path) -> int:
    """Stream a zip GET to dest, returning bytes written. Raises on transport error."""
    n = 0
    async with client.stream_zip(url) as resp:
        if resp.status_code == 502:
            return 0
        resp.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in resp.aiter_bytes(CHUNK):
                f.write(chunk)
                n += len(chunk)
    return n


async def pull(
    client: ScopeClient,
    obs: Observation,
    dest_zip: Path,
    *,
    fmt: str = "tiff",
    build_timeout: float | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Pull one observation's zip to dest_zip using the build-then-download flow.

    Yields ProgressEvents; raises TransferError on failure. Writes via a
    .partial file so dest_zip only ever exists complete and frame-bearing.
    """
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    partial = dest_zip.with_suffix(dest_zip.suffix + ".partial")
    url = f"/api/observations/zip/{fmt}/0x0/{obs.vpath}"
    timeout = build_timeout if build_timeout is not None else _build_timeout(obs.frame_count)

    # 1. Clear any stale prior job exactly once (never again during the build).
    await client.cancel_download()

    try:
        async with client.event_pump() as pump:
            # Ensure the pump has made contact (the export gate needs an active
            # poller) before we trigger the build.
            await pump.wait_ready(timeout=40)

            # 2. Trigger the build. For a large export this returns the instant
            #    empty zip; for a tiny/already-built one it may return frames.
            yield ProgressEvent(obs.obs_id, "trigger")
            first = await _stream_to(client, url, partial)
            if first >= EMPTY_ZIP_MAX and zip_has_frames(partial):
                partial.replace(dest_zip)
                yield ProgressEvent(obs.obs_id, "done", bytes_done=first)
                return
            partial.unlink(missing_ok=True)

            # 3. Wait for the build: "started" then "ended". No cancelling.
            await pump.wait_for_started(STARTED_TIMEOUT)
            yield ProgressEvent(
                obs.obs_id,
                "building",
                frames_done=pump.progress.frames_done,
                frames_total=pump.progress.frames_total,
            )
            ended = await pump.wait_for_ended(timeout)
            if not ended:
                raise TransferError(
                    f"{obs.target}: build did not finish within {timeout:.0f}s "
                    f"(reached {pump.progress.frames_done}/{pump.progress.frames_total} frames)"
                )
            yield ProgressEvent(
                obs.obs_id,
                "building",
                detail="ended",
                frames_done=pump.progress.frames_total,
                frames_total=pump.progress.frames_total,
            )

            # 4. Download the finished archive.
            yield ProgressEvent(obs.obs_id, "downloading")
            n = await _stream_to(client, url, partial)
    except PumpDead as e:
        partial.unlink(missing_ok=True)
        await client.cancel_download()
        raise TransferError(f"{obs.target}: {e}") from e
    except httpx.TransportError as e:
        partial.unlink(missing_ok=True)
        await client.cancel_download()
        raise TransferError(f"{obs.target}: transfer dropped ({type(e).__name__})") from e

    # 5. Validate + commit.
    if n < EMPTY_ZIP_MAX or not zip_has_frames(partial):
        partial.unlink(missing_ok=True)
        await client.cancel_download()
        raise TransferError(
            f"{obs.target}: download after 'ended' had no frames "
            f"({n} bytes) — scope may have dropped the job"
        )
    partial.replace(dest_zip)
    yield ProgressEvent(obs.obs_id, "done", bytes_done=n)
