#!/usr/bin/env python3
"""Pull ONE observation as TIFF and report frame geometry, for recon.

Usage:  uv run python scripts/pull_one.py <ip> <target-substring> [format]
  e.g.  uv run python scripts/pull_one.py 192.168.100.1 M51
        uv run python scripts/pull_one.py 192.168.100.1 "M101" tiff

Picks the first observation whose nameTarget (or vpath) matches the substring,
pulls it with the event pump running, saves the zip under
tests/fixtures/live/<name>.zip, and prints each frame's width x height x
bits-per-sample read straight from the TIFF header (pure stdlib — no decode).
Send the saved zip back for full pixel analysis.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import struct
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx

OUT = Path("tests/fixtures/live")


def tiff_geometry(blob: bytes) -> str:
    """Return 'WxH  Nbit  (compression C)' from a TIFF's first IFD. Stdlib only."""
    try:
        bo = "<" if blob[:2] == b"II" else ">"
        off = struct.unpack(bo + "I", blob[4:8])[0]
        n = struct.unpack(bo + "H", blob[off : off + 2])[0]
        tags: dict[int, int] = {}
        for i in range(n):
            e = blob[off + 2 + i * 12 : off + 2 + i * 12 + 12]
            tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
            if typ == 3:  # SHORT
                tags[tag] = struct.unpack(bo + "H", e[8:10])[0]
            elif typ == 4:  # LONG
                tags[tag] = struct.unpack(bo + "I", e[8:12])[0]
        w, h = tags.get(256, 0), tags.get(257, 0)
        bits = tags.get(258, 0)
        comp = tags.get(259, 0)
        spp = tags.get(277, 1)
        return f"{w}x{h}  {bits}-bit  samples/px={spp}  compression={comp}"
    except Exception as e:  # noqa: BLE001
        return f"(could not parse TIFF header: {e})"


async def pump_loop(http: httpx.AsyncClient, stop: asyncio.Event) -> None:
    first = True
    while not stop.is_set():
        try:
            await http.get("/api/event", timeout=httpx.Timeout(5 if first else 35, connect=10))
        except httpx.ReadTimeout:
            pass
        except httpx.TransportError:
            await asyncio.sleep(2)
        first = False


async def send_cancel(http: httpx.AsyncClient) -> None:
    try:
        await http.post("/api/event", json={"cmd": "cancelDownload"}, timeout=httpx.Timeout(5, connect=3))
    except httpx.TransportError:
        pass


async def main(ip: str, needle: str, fmt: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=f"http://{ip}", timeout=httpx.Timeout(300, connect=10)) as http:
        r = await http.get("/api/observations/list", timeout=httpx.Timeout(180, connect=10))
        items = json.loads(re.sub(r"\bNaN\b", "null", r.text))
        matches = [
            o for o in items
            if needle.lower() in str(o.get("nameTarget", "")).lower()
            or needle.lower() in str(o.get("vpath", "")).lower()
        ]
        # Smallest frame count first — never accidentally pull a 1000+ frame run.
        matches.sort(key=lambda o: o.get("nb_frames") or 0)
        match = matches[0] if matches else None
        if len(matches) > 1:
            print(f"({len(matches)} matched {needle!r}; picking smallest: "
                  f"{matches[0].get('nb_frames')} frames)")
        if not match:
            print(f"No observation matched {needle!r}. Targets available:")
            for o in items:
                print(f"  {o.get('nameTarget')!r:40} {o.get('pmode')} "
                      f"frames={o.get('nb_frames')} {o.get('vpath')}")
            sys.exit(2)

        target = str(match.get("nameTarget", "obs"))
        pmode = match.get("pmode")
        vpath = str(match["vpath"])
        print(f"Matched: {target!r} [{pmode}] frames={match.get('nb_frames')} "
              f"resx={match.get('resx')} resy={match.get('resy')} depth={match.get('depth')}")
        print(f"Pulling {fmt.upper()} of {vpath} ...")

        # Retry loop with escalating warm-up — EnhancedVision exports return an
        # instant empty zip (PK\x05\x06 + padding) until the backend has prepared
        # the job. Prior art needed up to ~10 tries with growing settle. We cancel
        # any stale job before each attempt and keep the event pump actively polling.
        url = f"/api/observations/zip/{fmt}/0x0/{quote(vpath, safe='/')}"
        MAX_ATTEMPTS = 8
        data = b""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await send_cancel(http)
            await asyncio.sleep(1)
            stop = asyncio.Event()
            task = asyncio.create_task(pump_loop(http, stop))
            settle = 6 + attempt * 3  # 9,12,15,... seconds — grows each retry
            print(f"  attempt {attempt}/{MAX_ATTEMPTS}: pump warming {settle}s ...", flush=True)
            await asyncio.sleep(settle)

            t0 = time.time()
            buf = io.BytesIO()
            try:
                async with http.stream("GET", url) as resp:
                    ct = resp.headers.get("content-type", "")
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        buf.write(chunk)
            except httpx.TransportError as e:
                print(f"    dropped: {type(e).__name__}")
            finally:
                stop.set()
                task.cancel()

            data = buf.getvalue()
            dt = time.time() - t0
            # Empty export = the ~10KB PK\x05\x06+padding zip. Detect and retry.
            is_empty = len(data) < 20000 and data[:4] == b"PK\x05\x06"
            print(f"    HTTP {resp.status_code} {ct} {len(data)} bytes in {dt:.1f}s"
                  f"{' [EMPTY]' if is_empty else ''}")
            if data and not is_empty:
                break
            if attempt < MAX_ATTEMPTS:
                print("    empty — retrying with longer warm-up")
        await send_cancel(http)

        safe = re.sub(r"[^A-Za-z0-9]+", "_", target).strip("_") or "obs"
        dest = OUT / f"{safe}_{fmt}.zip"
        dest.write_bytes(data)
        print(f"  {len(data)} bytes in {time.time()-t0:.1f}s -> {dest}")

        try:
            with zipfile.ZipFile(dest) as z:
                frames = [n for n in z.namelist()
                          if not n.endswith("/") and not n.endswith("manifest.json")]
                print(f"  zip: {len(z.namelist())} entries, {len(frames)} frame(s)")
                if not frames:
                    print("  ⚠ MANIFEST-ONLY (empty export — retry; scope may be busy/full)")
                for fn in frames[:4]:
                    fb = z.read(fn)
                    geo = tiff_geometry(fb) if fn.lower().endswith((".tif", ".tiff")) else \
                          f"{len(fb)} bytes (not TIFF)"
                    print(f"    {fn.split('/')[-1]}: {geo}")
        except zipfile.BadZipFile:
            print("  ⚠ not a valid zip")

    print(f"\nSend me {dest} for full pixel analysis (bit range + Bayer test).")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: pull_one.py <ip> <target-substring> [fits|tiff|png]")
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "tiff"))
