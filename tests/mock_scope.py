"""FastAPI fake of the Odyssey DDD backend, faithful to docs/API.md.

Reproduces the behaviors scopepull's reliability logic depends on:
- the long-poll GATE: zip needs a fresh /api/event poll
- the WARM-UP: the first N zip requests for a job return an instant empty zip
  (PK\\x05\\x06 + padding), like an EnhancedVision export that isn't ready yet;
  attempt N+1 returns the real calibration-set zip
- NaN tokens, DDD-disabled, stuck-job, manifest-only, flaky (drop at N bytes)
- real zip layout: StackInput (raw Bayer lights) + DarkframeMean + StackSum +
  preview.jpg + manifest.json
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile

import numpy as np
import tifffile
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

POLL_FRESHNESS = 5.0

SPA_HTML = "<html><head><title>Unistellar</title></head><body>vue app</body></html>"

# An empty export: End-Of-Central-Directory record + zero padding to 10240 bytes,
# exactly what the real scope returns for a not-ready/failed export.
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * (10240 - 4)

OBSERVATIONS = [
    {
        "vpath": "prod/obs-0001",
        "name": "20260201T050025_166",
        "nameTarget": "M101 - Pinwheel Galaxy",
        "purpose": "Default",
        "pmode": "EnhancedVision",
        "type": "BAYER_GBRG",
        "obs_start": 1787184000000,
        "obs_end": 1787184050000,
        "nb_frames": 6,
        "expo": 3999977,
        "gain": 321,
        "ra": 210.8,
        "dec": 54.3,
        "resx": 2904,
        "resy": 2192,
        "depth": 12,
        "sensor": "IMX415",
        "obs_attr": {"frames_saved": 2, "frames_stacked": 1},
    },
    {
        "vpath": "prod/obs-0002",
        "name": "20260202T023416_000",
        "nameTarget": "Jupiter",
        "purpose": "Default",
        "pmode": "PlanetEV",
        "type": "BAYER_GBRG",
        "obs_start": 1787270400000,
        "obs_end": 1787270450000,
        "nb_frames": 2,
        "expo": 14699,
        "gain": 0,
        "ra": 108.3,
        "dec": 22.6,
        "resx": 2904,
        "resy": 2192,
        "depth": 12,
        "sensor": "IMX415",
        "obs_attr": {"frames_saved": 2, "frames_stacked": 16, "tag_sc": "NAN_SENTINEL"},
    },
]


def _bayer_frame(seed: int, w: int = 160, h: int = 120) -> bytes:
    """A tiny 16-bit TIFF with a real GBRG-ish Bayer phase difference, LZW."""
    rng = np.random.default_rng(seed)
    a = rng.integers(2000, 3000, size=(h, w), dtype=np.uint16)
    # Impose a per-phase offset so the 2x2 phase spread reads as a Bayer mosaic.
    a[0::2, 0::2] += 8000  # G
    a[0::2, 1::2] += 12000  # B
    a[1::2, 0::2] += 4000  # R
    a[1::2, 1::2] += 8000  # G
    buf = io.BytesIO()
    tifffile.imwrite(buf, a, compression="lzw")
    return buf.getvalue()


def _mono_frame(seed: int, w: int = 160, h: int = 120) -> bytes:
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 60000, size=(h, w), dtype=np.uint16)
    buf = io.BytesIO()
    tifffile.imwrite(buf, a, compression="lzw")
    return buf.getvalue()


def make_calibration_zip(obs: dict, *, manifest_only: bool = False) -> bytes:
    """A real-shape EnhancedVision export: lights + dark + stacksum + preview."""
    name = obs["name"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/manifest.json", json.dumps(obs))
        if manifest_only:
            return buf.getvalue()
        n = int(obs["obs_attr"].get("frames_saved", 2))
        for i in range(n):
            z.writestr(f"{name}/{name[:-4]}{i:02d}_StackInput.tiff", _bayer_frame(i + 1))
        z.writestr(f"{name}/{name}_DarkframeMean.tiff", _bayer_frame(100))
        z.writestr(f"{name}/{name}_StackSum.tiff", _mono_frame(200))
        z.writestr(f"{name}/preview.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 512)
    return buf.getvalue()


def create_app() -> FastAPI:
    app = FastAPI()
    state = {
        "last_poll": 0.0,
        "ddd_enabled": True,
        "stuck_job": False,
        "manifest_only": False,
        "flaky_after": 0,
        "warmup_empties": 0,  # first N zip GETs (job lifetime) return empty, then real
        "events": [],
        "cancel_count": 0,
        "warmup_seen": 0,  # total real zip GETs; NOT reset by cancel (readies over time)
    }
    app.state.scope = state

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return SPA_HTML

    @app.get("/api/observations/list")
    async def listing() -> Response:
        if not state["ddd_enabled"]:
            return HTMLResponse(SPA_HTML)
        body = json.dumps(OBSERVATIONS).replace('"NAN_SENTINEL"', "NaN")
        return Response(content=body, media_type="application/json")

    @app.get("/api/event")
    async def event_poll() -> Response:
        state["last_poll"] = time.monotonic()
        if state["events"]:
            return Response(
                content=json.dumps(state["events"].pop(0)), media_type="application/json"
            )
        # Hold briefly like the real long-poll so the pump paces itself and
        # doesn't saturate the shared ASGI transport during a concurrent stream.
        await asyncio.sleep(0.05)
        return Response(content="", media_type="text/plain")

    @app.post("/api/event")
    async def event_cmd(request: Request) -> dict:
        data = await request.json()
        if data.get("cmd") == "cancelDownload":
            state["stuck_job"] = False
            state["cancel_count"] += 1
        return {"ok": True}

    @app.get("/api/observations/zip/{fmt}/{res}/{vpath:path}")
    async def zip_download(fmt: str, res: str, vpath: str) -> Response:
        if not state["ddd_enabled"]:
            return HTMLResponse(SPA_HTML)
        if time.monotonic() - state["last_poll"] > POLL_FRESHNESS:
            return Response(status_code=502, content="Bad Gateway")
        if state["stuck_job"]:
            return Response(status_code=502, content="Bad Gateway")
        obs = next((o for o in OBSERVATIONS if o["vpath"] == vpath), None)
        if obs is None:
            return Response(status_code=404)

        # Warm-up: first N zip GETs over the job's life return the empty zip, then
        # the real export — models "export readies over time despite cancels".
        state["warmup_seen"] += 1
        if state["warmup_seen"] <= state["warmup_empties"]:
            return Response(content=EMPTY_ZIP, media_type="application/zip")

        blob = make_calibration_zip(obs, manifest_only=state["manifest_only"])

        if state["flaky_after"] > 0:
            cut = state["flaky_after"]

            async def dribble():
                # Send fewer bytes than Content-Length promises, then stop —
                # httpx sees a RemoteProtocolError (peer closed mid-body).
                yield blob[:cut]

            return StreamingResponse(
                dribble(), media_type="application/zip", headers={"Content-Length": str(len(blob))}
            )

        return Response(content=blob, media_type="application/zip")

    return app
