"""FastAPI fake of the Odyssey DDD backend, faithful to docs/API.md.

Models the real export protocol cracked from HAR captures: a zip GET triggers a
server-side build that advances one frame per /api/event poll, reporting
status "started" with climbing progress, then a terminal "ended"; only after
"ended" does a zip GET return the real calibration-set archive.

Also reproduces: the long-poll gate, NaN tokens, DDD-disabled, manifest-only,
and a flaky short-read.
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

# A not-ready export: End-Of-Central-Directory record + zero padding to 10240 B,
# exactly what the real scope returns while an export is still building.
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
    rng = np.random.default_rng(seed)
    a = rng.integers(2000, 3000, size=(h, w), dtype=np.uint16)
    a[0::2, 0::2] += 8000
    a[0::2, 1::2] += 12000
    a[1::2, 0::2] += 4000
    a[1::2, 1::2] += 8000
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
        "manifest_only": False,
        "flaky_after": 0,
        "cancel_count": 0,
        # Build state machine: a zip GET triggers a build that advances one
        # frame per event poll; "ended" fires when complete; only then does a
        # zip GET return the real archive.
        "build": "idle",  # idle | building | ready
        "build_vpath": None,
        "progress": 0,
        "build_frames": 3,  # frames this export needs (small for fast tests)
        "emitted_ended": False,
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
        await asyncio.sleep(0.02)  # brief long-poll hold; paces the pump
        if state["build"] == "building":
            state["progress"] += 1
            if state["progress"] >= state["build_frames"]:
                state["build"] = "ready"
            body = {
                "cmd": "download",
                "status": "started",
                "progress": min(state["progress"], state["build_frames"]),
                "nb_frames": state["build_frames"],
            }
            return Response(content=json.dumps(body), media_type="application/json")
        if state["build"] == "ready" and not state["emitted_ended"]:
            state["emitted_ended"] = True
            body = {"cmd": "download", "status": "ended", "progress": 0, "nb_frames": 0}
            return Response(content=json.dumps(body), media_type="application/json")
        return Response(content="", media_type="text/plain")

    @app.post("/api/event")
    async def event_cmd(request: Request) -> dict:
        data = await request.json()
        if data.get("cmd") == "cancelDownload":
            state["cancel_count"] += 1
            state["build"] = "idle"
            state["build_vpath"] = None
            state["progress"] = 0
            state["emitted_ended"] = False
        return {"ok": True}

    @app.get("/api/observations/zip/{fmt}/{res}/{vpath:path}")
    async def zip_download(fmt: str, res: str, vpath: str) -> Response:
        if not state["ddd_enabled"]:
            return HTMLResponse(SPA_HTML)
        if time.monotonic() - state["last_poll"] > POLL_FRESHNESS:
            return Response(status_code=502, content="Bad Gateway")
        obs = next((o for o in OBSERVATIONS if o["vpath"] == vpath), None)
        if obs is None:
            return Response(status_code=404)

        if state["build"] == "ready" and state["build_vpath"] == vpath:
            pass  # fall through: return the real archive
        else:
            # Trigger (or continue) the build; return the instant empty zip.
            if state["build"] != "building" or state["build_vpath"] != vpath:
                state["build"] = "building"
                state["build_vpath"] = vpath
                state["progress"] = 0
                state["emitted_ended"] = False
            return Response(content=EMPTY_ZIP, media_type="application/zip")

        blob = make_calibration_zip(obs, manifest_only=state["manifest_only"])

        if state["flaky_after"] > 0:
            cut = state["flaky_after"]

            async def dribble():
                yield blob[:cut]  # short read vs Content-Length -> client error

            return StreamingResponse(
                dribble(),
                media_type="application/zip",
                headers={"Content-Length": str(len(blob))},
            )

        return Response(content=blob, media_type="application/zip")

    return app
