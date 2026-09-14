"""Streaming zip transfer using the scope's real export protocol.

Reverse-engineered from the Vue app's own code (docs/API.md): downloading is a
SINGLE held GET to the zip URL. That one request triggers the server-side build
AND streams the finished archive — the scope sends no body bytes while it is
still building (which can take many minutes), then streams the whole zip. A
concurrent event-poll must be active for the scope to stream at all (the export
gate); the /api/event channel reports build progress (status "started",
progress 1..nb_frames, then "ended") purely for display.

Two hard-won rules, both proven live against the scope:
  * DO NOT cancelDownload before the GET — a pre-cancel makes the scope return
    an instant empty zip instead of building. The browser never cancels.
  * The GET's read timeout must exceed the whole build time (no bytes flow
    during the build), so it is scaled to the frame count.

Nothing here prints; it yields ProgressEvent objects the CLI renders.
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import Observation
from .client import PumpDead, ScopeClient
from .fsutil import replace_retry

CHUNK = 256 * 1024
# A not-ready/failed export is a ~10 KB empty zip (PK\x05\x06 + padding).
EMPTY_ZIP_MAX = 20_000
# Build runs ~1 frame/sec and streams no bytes until done; give the held GET a
# read timeout well beyond that, floored and generously scaled by frame count.
PER_FRAME_TIMEOUT = 5.0
MIN_READ_TIMEOUT = 300.0
# Seconds to let the event pump issue its first poll (open the gate) before the
# GET. We do NOT wait for a poll *response* — an idle poll long-holds ~30s.
GATE_WARMUP = 2.5


class TransferError(Exception):
    """A pull that could not complete."""


@dataclass
class ProgressEvent:
    obs_id: str
    phase: str  # "starting" | "downloading" | "done" | "failed"
    bytes_done: int = 0
    frames_done: int = 0
    frames_total: int = 0
    elapsed_s: float = 0.0
    detail: str = ""


def _read_timeout(nb_frames: int) -> float:
    return max(MIN_READ_TIMEOUT, nb_frames * PER_FRAME_TIMEOUT)


def zip_has_frames(path: Path) -> bool:
    """True iff the zip is valid AND holds >=1 non-manifest member."""
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
    read_timeout: float | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Pull one observation's zip to dest_zip via the single-held-GET protocol.

    Yields ProgressEvents; raises TransferError on failure. Writes via a
    .partial file so dest_zip only ever exists complete and frame-bearing.
    """
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    partial = dest_zip.with_suffix(dest_zip.suffix + ".partial")
    url = f"/api/observations/zip/{fmt}/0x0/{obs.vpath}"
    rt = read_timeout if read_timeout is not None else _read_timeout(obs.frame_count)
    n = 0

    try:
        # Event pump = the export gate + the progress source. NO pre-cancel.
        async with client.event_pump() as pump:
            # Let the pump issue its first poll (open the gate); don't await a
            # response — an idle event poll long-holds ~30s.
            await asyncio.sleep(GATE_WARMUP)

            yield ProgressEvent(obs.obs_id, "starting", frames_total=obs.frame_count)
            start = time.monotonic()
            async with client.stream_zip(url, read_timeout=rt) as resp:
                resp.raise_for_status()
                with partial.open("wb") as f:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        f.write(chunk)
                        n += len(chunk)
                        yield ProgressEvent(
                            obs.obs_id,
                            "downloading",
                            bytes_done=n,
                            frames_done=pump.progress.frames_done,
                            frames_total=pump.progress.frames_total or obs.frame_count,
                            elapsed_s=time.monotonic() - start,
                        )
    except (httpx.TransportError, httpx.HTTPStatusError, PumpDead) as e:
        partial.unlink(missing_ok=True)
        # Only NOW is a cancel appropriate — to clear the half-built job.
        await client.cancel_download()
        raise TransferError(f"{obs.target}: transfer failed ({type(e).__name__})") from e

    # Validate: a real, frame-bearing archive (not the empty/manifest-only zip).
    if n < EMPTY_ZIP_MAX or not zip_has_frames(partial):
        partial.unlink(missing_ok=True)
        await client.cancel_download()
        raise TransferError(
            f"{obs.target}: export returned no frames ({n} bytes) — the scope may "
            "be busy or a stale job was active; try again"
        )
    # Defender may still be scanning the file we just closed; wait it out.
    replace_retry(partial, dest_zip)
    yield ProgressEvent(obs.obs_id, "done", bytes_done=n)
