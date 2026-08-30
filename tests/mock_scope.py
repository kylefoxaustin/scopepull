"""FastAPI fake of the Odyssey DDD backend, faithful to docs/API.md:

- the long-poll GATE: zip endpoint 502s unless a client polled /api/event
  within the last few seconds
- NaN tokens in the listing body
- DDD-disabled state (serves the SPA html on API paths)
- stuck-job state (zip 502s until cancelDownload arrives)
- manifest-only mode (the Odyssey FITS firmware bug)
- flaky mode (drops the connection after N bytes)
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

POLL_FRESHNESS = 5.0  # zip streams only if an event poll happened this recently

SPA_HTML = "<html><head><title>Unistellar</title></head><body>vue app</body></html>"

OBSERVATIONS = [
    {
        "vpath": "obs/2026-08-20/0001",
        "name": "Bubble Nebula",
        "purpose": "science",
        "pmode": "Enhanced Vision",
        "obs_start": 1787184000000,
        "nb_frames": 1200,
        "obs_attr": {"tag_sc": "NGC 7635"},
    },
    {
        "vpath": "obs/2026-08-21/0002",
        "name": "M 31",
        "purpose": "",
        "pmode": "Enhanced Vision",
        "obs_start": 1787270400000,
        "nb_frames": 300,
        "obs_attr": {"tag_sc": "M 31"},
    },
    {
        # exercises NaN + missing fields
        "vpath": "obs/2026-08-22/0003",
        "name": "",
        "purpose": "calibration",
        "pmode": "Live",
        "obs_start": None,
        "nb_frames": "NAN_SENTINEL",
        "obs_attr": {},
    },
]


def make_zip(fmt: str, vpath: str, *, manifest_only: bool = False, frames: int = 3) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"vpath": vpath, "format": fmt}))
        if not manifest_only:
            for i in range(frames):
                z.writestr(f"frame_{i:04d}.{fmt}", b"\x00" * 2048 + bytes([i]))
    return buf.getvalue()


def create_app() -> FastAPI:
    app = FastAPI()
    state = {
        "last_poll": 0.0,  # monotonic time of last /api/event GET
        "ddd_enabled": True,
        "stuck_job": False,  # cleared by cancelDownload
        "manifest_only": False,  # the Odyssey FITS bug
        "flaky_after": 0,  # >0: drop connection after N bytes
        "events": [],  # queued event messages to hand to pollers
        "cancel_count": 0,
    }
    app.state.scope = state

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return SPA_HTML

    @app.get("/api/observations/list")
    async def listing() -> Response:
        if not state["ddd_enabled"]:
            return HTMLResponse(SPA_HTML)
        # Emit bare NaN exactly like the real scope does.
        body = json.dumps(OBSERVATIONS).replace('"NAN_SENTINEL"', "NaN")
        return Response(content=body, media_type="application/json")

    @app.get("/api/event")
    async def event_poll() -> Response:
        state["last_poll"] = time.monotonic()
        if state["events"]:
            return Response(
                content=json.dumps(state["events"].pop(0)),
                media_type="application/json",
            )
        # Real scope holds ~30s; hold briefly so tests stay fast but the
        # "connection held open" behavior is representable.
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
        # THE GATE: no fresh event poll -> 502, exactly like evsoft.
        if time.monotonic() - state["last_poll"] > POLL_FRESHNESS:
            return Response(status_code=502, content="Bad Gateway")
        if state["stuck_job"]:
            return Response(status_code=502, content="Bad Gateway")
        if not any(o["vpath"] == vpath for o in OBSERVATIONS):
            return Response(status_code=404)

        blob = make_zip(fmt, vpath, manifest_only=state["manifest_only"])

        if state["flaky_after"] > 0:
            cut = state["flaky_after"]

            async def dribble():
                yield blob[:cut]
                raise ConnectionResetError("mock scope dropped the link")

            return StreamingResponse(
                dribble(),
                media_type="application/zip",
                headers={"Content-Length": str(len(blob))},
            )

        return Response(content=blob, media_type="application/zip")

    return app
