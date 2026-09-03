#!/usr/bin/env python3
"""Phase 0 live recon against a real scope. Run: uv run python scripts/live_recon.py <ip>

Captures, in order (read-only except for the zip pulls themselves):
  1. GET /                      -> fixtures: headers + first 2KB
  2. GET /api/observations/list -> RAW body saved verbatim (the fixture)
  3. /api/event idle poll x3    -> timing + any messages
  4. cancelDownload             -> response shape
  5. Smallest observation, FITS -> the firmware-bug test (zip saved if small)
  6. Same observation, PNG      -> control (prior art says PNG works)
Everything lands in tests/fixtures/live/ with a capture log. Scrub before commit.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx

OUT = Path("tests/fixtures/live")
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


async def send_cancel(http: httpx.AsyncClient) -> str:
    """Fire-and-forget cancelDownload. LIVE FINDING (2026-08-30): the scope
    does NOT answer this POST when there is no job to cancel — it just holds
    the connection. Short timeout, swallow everything."""
    try:
        r = await http.post("/api/event", json={"cmd": "cancelDownload"},
                            timeout=httpx.Timeout(5, connect=3))
        return f"{r.status_code} {r.text[:200]!r}"
    except httpx.TransportError as e:
        return f"no answer ({type(e).__name__}) — normal when no job is active"


async def main(ip: str) -> None:
    # Preflight: fail with one friendly line, not a traceback, when we're not
    # on the scope's network (the #1 way this script gets run wrong).
    import sys as _sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from scopepull.netcheck import check as netcheck_check

    net = await netcheck_check(ip)
    if not net.reachable:
        print(f"✗ Scope at {ip} is not reachable.")
        print(f"  {net.diagnosis(ip)}")
        _sys.exit(3)

    OUT.mkdir(parents=True, exist_ok=True)
    base = f"http://{ip}"
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(30, connect=10)) as http:
        # 1. root
        r = await http.get("/")
        log(f"GET / -> {r.status_code} {r.headers.get('content-type')} server={r.headers.get('server')!r}")
        (OUT / "root_headers.json").write_text(json.dumps(dict(r.headers, encoding="utf-8"), indent=2))
        (OUT / "root_body_head.html").write_text(r.text[:2048], encoding="utf-8")

        # 2. listing — raw bytes, verbatim
        log("GET /api/observations/list (can take a while)...")
        t0 = time.monotonic()
        r = await http.get("/api/observations/list", timeout=httpx.Timeout(180, connect=10))
        dt = time.monotonic() - t0
        log(f"  -> {r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes in {dt:.1f}s")
        (OUT / "observations_list.raw.json").write_bytes(r.content)
        has_nan = b"NaN" in r.content
        log(f"  contains bare NaN: {has_nan}")
        obs = None
        if r.status_code == 200:
            try:
                import re
                data = json.loads(re.sub(rb"\bNaN\b", b"null", r.content))
                items = data if isinstance(data, list) else []
                log(f"  parsed: {len(items)} observations; keys of first: "
                    f"{sorted(items[0].keys()) if items else 'n/a'}")
                # pick the smallest by nb_frames for the pull test
                withframes = [o for o in items if isinstance(o.get("nb_frames"), (int, float))
                              and o.get("vpath")]
                obs = min(withframes, key=lambda o: o["nb_frames"]) if withframes else None
                if obs:
                    log(f"  smallest obs: vpath={obs['vpath']!r} nb_frames={obs['nb_frames']}")
            except Exception as e:
                log(f"  parse failed: {e!r}")

        # 3. idle event polls
        for i in range(3):
            t0 = time.monotonic()
            try:
                r = await http.get("/api/event", timeout=httpx.Timeout(8, connect=10))
                dt = time.monotonic() - t0
                log(f"GET /api/event [{i}] -> {r.status_code} in {dt:.2f}s body={r.text[:200]!r}")
                (OUT / f"event_idle_{i}.txt").write_text(
                    f"{r.status_code} in {dt:.2f}s\n{r.text[:4096]}", encoding="utf-8")
            except httpx.ReadTimeout:
                dt = time.monotonic() - t0
                log(f"GET /api/event [{i}] -> READ TIMEOUT after {dt:.2f}s (long-poll held)")

        # 4. cancelDownload response shape (hangs when no job — expected)
        result = await send_cancel(http)
        log(f"POST cancelDownload -> {result}")
        (OUT / "cancel_response.txt").write_text(result, encoding="utf-8")

        if not obs:
            log("no observation available for pull test; done")
            return

        # 5+6. the FITS-bug test, then PNG control
        from urllib.parse import quote
        vp = quote(str(obs["vpath"]), safe="/")
        for fmt in ("fits", "tiff", "png"):
            log(f"--- pull test: {fmt.upper()} of {obs['vpath']!r} ---")
            log(f"  cancel: {await send_cancel(http)}")
            await asyncio.sleep(1)
            stop = asyncio.Event()

            async def pump() -> None:
                first = True
                while not stop.is_set():
                    try:
                        rr = await http.get("/api/event",
                                            timeout=httpx.Timeout(5 if first else 35, connect=10))
                        if rr.text.strip():
                            log(f"  [event] {rr.text[:300]!r}")
                            (OUT / f"event_during_{fmt}.log").open("a", encoding="utf-8").write(rr.text[:2048] + "\n")
                    except httpx.ReadTimeout:
                        pass
                    except httpx.TransportError as e:
                        log(f"  [event] transport error: {e!r}")
                        await asyncio.sleep(2)
                    first = False

            task = asyncio.create_task(pump())
            await asyncio.sleep(8)  # first poll ack + settle, generous
            dest = OUT / f"pull_test.{fmt}.zip"
            try:
                t0 = time.monotonic()
                async with http.stream(
                    "GET", f"/api/observations/zip/{fmt}/0x0/{vp}",
                    timeout=httpx.Timeout(300, connect=15),
                ) as resp:
                    log(f"  -> {resp.status_code} ct={resp.headers.get('content-type')} "
                        f"cl={resp.headers.get('content-length')}")
                    n = 0
                    with dest.open("wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            f.write(chunk)
                            n += len(chunk)
                    dt = time.monotonic() - t0
                    log(f"  {n} bytes in {dt:.1f}s ({n/dt/1024:.0f} KB/s)")
            except Exception as e:
                log(f"  pull failed: {e!r}")
                stop.set(); task.cancel()
                continue
            stop.set(); task.cancel()

            # verdict
            try:
                with zipfile.ZipFile(dest) as z:
                    names = z.namelist()
                    nonmanifest = [x for x in names
                                   if not x.endswith("/") and not x.endswith("manifest.json")]
                    log(f"  zip: {len(names)} entries, {len(nonmanifest)} data files")
                    log(f"  entries head: {names[:8]}")
                    if "manifest.json" in names or any(x.endswith("manifest.json") for x in names):
                        mf = next(x for x in names if x.endswith("manifest.json"))
                        (OUT / f"manifest_{fmt}.json").write_bytes(z.read(mf))
                        log(f"  saved {mf} -> manifest_{fmt}.json")
                    verdict = "HAS FRAME DATA ✓" if nonmanifest else "MANIFEST-ONLY ✗ (the bug)"
                    log(f"  VERDICT [{fmt}]: {verdict}")
                    # keep first FITS frame for header recon
                    fits_frames = [x for x in nonmanifest if x.lower().endswith((".fits", ".fit"))]
                    if fits_frames:
                        (OUT / "sample_frame.fits").write_bytes(z.read(fits_frames[0]))
                        log(f"  saved sample frame: {fits_frames[0]}")
            except zipfile.BadZipFile:
                log(f"  VERDICT [{fmt}]: NOT A ZIP ✗ ({dest.stat().st_size} bytes)")

        await send_cancel(http)
    (OUT / "capture.log").write_text("\n".join(LOG, encoding="utf-8") + "\n")
    log(f"recon complete -> {OUT}/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: live_recon.py <scope-ip>")
    asyncio.run(main(sys.argv[1]))
