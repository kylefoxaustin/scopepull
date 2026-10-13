"""The Unistellar Odyssey Pro: a web API behind the scope's own Wi-Fi.

An adapter, not a rewrite. `find` is the listing call the CLI used to make
inline, and `pull` is the loop that used to live in `cli._pull`, unchanged
apart from narrating through a `Log` instead of printing straight to the
console. This is the one path in scopepull that has been proved against real
hardware over a season of nights -- the point of moving it behind the Scope
protocol is that the Seestar can stand next to it, not that it should behave
any differently.

Odyssey archive layout (unchanged, and starstack reads it -- docs/STARSTACK.md):

    <root>/<night>/<target>__<id>/frames|calibration|reference|observation.json
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..catalog import Observation, filter_observations
from ..client import DDDNotEnabled, ScopeClient, ScopeUnreachable
from ..config import Config
from ..fsutil import unlink_retry
from ..ingest import ingest_zip
from ..log import Log
from ..manifest import Manifest
from ..transfer import ProgressEvent, TransferError, zip_has_frames
from ..transfer import pull as transfer_pull
from . import (
    EXIT_DDD_DISABLED,
    EXIT_NOTHING_NEW,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_UNREACHABLE,
    LABELS,
    Found,
    PullResult,
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


async def _reuse_zip(obs: Observation, zip_path: Path) -> AsyncIterator[ProgressEvent]:
    """Stand-in for transfer_pull when the zip is already here and valid."""
    n = zip_path.stat().st_size
    yield ProgressEvent(obs.obs_id, "already downloaded", bytes_done=n)
    yield ProgressEvent(obs.obs_id, "done", bytes_done=n)


def build_status(ev: object) -> str:
    """Live line: 'building 187/364 frames · 3m20s' while the scope builds the
    export, then 'downloading 210MB · 512KB/s' once the archive streams."""
    done = getattr(ev, "frames_done", 0)
    total = getattr(ev, "frames_total", 0)
    elapsed = getattr(ev, "elapsed_s", 0.0)
    nbytes = getattr(ev, "bytes_done", 0)

    def _dur(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"

    # No body bytes flow until the build finishes, so bytes>threshold == streaming.
    if nbytes > 20_000:
        mb = nbytes / 2**20
        rate = nbytes / elapsed if elapsed > 0 else 0
        rate_s = f"{rate / 2**20:.1f}MB/s" if rate > 2**20 else f"{rate / 1024:.0f}KB/s"
        return f"downloading {mb:.0f}MB · {rate_s}"
    parts = []
    if total:
        parts.append(f"building {done}/{total} frames")
    else:
        parts.append("building")
    parts.append(_dur(elapsed))
    if done and total and elapsed > 2 and done < total:
        rate = done / elapsed
        if rate > 0:
            parts.append(f"~{_dur((total - done) / rate)} left")
    return " · ".join(parts)


class OdysseyScope:
    """`--scope odyssey` (and bare `scopepull`, which has always meant this)."""

    name = "odyssey"
    label = LABELS["odyssey"]

    def __init__(
        self,
        cfg: Config,
        *,
        new: bool = True,
        since: str | None = None,
        fmt: str | None = None,
        manifest_db: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.new = new
        self.since = since
        self.format = fmt or cfg.format
        # Only tests pass this; everything else wants the one real manifest.
        self.manifest_db = manifest_db

    def archive_root(self) -> Path:
        return self.cfg.archive_root

    def find(self, spec: str | None, log: Log) -> Found:
        """Ask the scope what it's holding. The listing is the probe: if it
        answers we're on its Wi-Fi and Direct Data Download is on, and if it
        doesn't, the exception says which of those is wrong."""
        if spec:
            self.cfg.scope_ip = spec

        async def go() -> list[Observation]:
            async with ScopeClient(self.cfg.base_url) as client:
                return await client.list_observations()

        try:
            observations = asyncio.run(go())
        except ScopeUnreachable as e:
            return Found(None, f"scope unreachable: {e}", EXIT_UNREACHABLE)
        except DDDNotEnabled as e:
            return Found(None, str(e), EXIT_DDD_DISABLED)
        return Found(
            observations,
            f"at {self.cfg.scope_ip}, {len(observations)} observation(s) on the scope",
        )

    def pull(self, handle: Any, root: Path, log: Log, target: str | None = None) -> PullResult:
        observations: list[Observation] = list(handle)
        # ingest_zip lays out the archive from cfg.archive_root, so --dest and a
        # per-scope root both arrive here as `root`.
        self.cfg.archive_root = root
        self.cfg.format = self.format
        observations = filter_observations(observations, self.since, target)

        with Manifest(self.manifest_db) as m:
            if self.new:
                local = m.local_ids()
                observations = [o for o in observations if o.obs_id not in local]
            if not observations:
                log("Nothing new to pull.")
                return PullResult(code=EXIT_NOTHING_NEW)
            return asyncio.run(self._pull_all(observations, root, log, m))

    async def _pull_all(
        self,
        observations: list[Observation],
        root: Path,
        log: Log,
        m: Manifest,
    ) -> PullResult:
        res = PullResult()
        log(f"Pulling {len(observations)} observation(s) to {root}")
        tmp_root = root / "_incoming"
        async with ScopeClient(self.cfg.base_url) as client:
            for i, obs in enumerate(observations, 1):
                zip_path = tmp_root / f"{obs.id_short}.zip"
                label = f"[{i}/{len(observations)}] {obs.target}"
                try:
                    last = ""
                    tick = 0
                    on_progress_line = False
                    # A complete, frame-bearing zip left over from a run that
                    # died after the download (ingest failure, crash) is
                    # reused, not re-fetched: 1.2 GB of NGC 7217 should be
                    # pulled once.
                    events = (
                        _reuse_zip(obs, zip_path)
                        if zip_path.exists() and zip_has_frames(zip_path)
                        else transfer_pull(client, obs, zip_path, fmt=self.format)
                    )
                    async for ev in events:
                        if ev.phase == "downloading":
                            tick += 1
                            # ljust pads over any longer previous line so it
                            # doesn't leave residue (the "donewnloading" bug).
                            line = f"  {label}: {SPINNER[tick % len(SPINNER)]} {build_status(ev)}"
                            log.raw(line.ljust(72) + "\r")
                            on_progress_line = True
                        elif ev.phase != last:
                            if on_progress_line:
                                log.raw("\n")  # finalize the in-place line
                                on_progress_line = False
                            log(f"  {label}: {ev.phase}")
                        last = ev.phase
                    if on_progress_line:
                        log.raw("\n")
                    try:
                        ingested = ingest_zip(zip_path, obs, self.cfg, m)
                    except Exception as e:  # one bad manifest must not end the night
                        log(
                            f"  {label}: ingest failed — {type(e).__name__}: {e} "
                            f"(zip kept at {zip_path}; retried next run)"
                        )
                        res.failed.append(obs.target)
                        continue
                    unlink_retry(zip_path)
                    log(
                        f"  {label}: ingested "
                        f"{ingested.light_count} frames, {ingested.fits_written} FITS"
                    )
                    res.copied += 1
                    res.nights.add(obs.date_str)
                except TransferError as e:
                    log(f"  {label}: failed — {e}")
                    res.failed.append(obs.target)
                    unlink_retry(zip_path)

        if res.failed:
            log(f"{len(res.failed)} failed (retry next run): {', '.join(res.failed)}")
            res.code = EXIT_PARTIAL
        else:
            res.code = EXIT_OK
        return res
