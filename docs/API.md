# Odyssey Pro DDD API — Phase 0 recon

Source: `github.com/vamshikesireddy/unistellar-data-downloader` (MIT, studied
2026-08-29, HEAD `6f663c0`), which reverse-engineered the Vue "Direct Data
Download" app at `http://192.168.100.1/`. Everything below is SOURCED from that
repo's code and commit history unless marked **VERIFY-LIVE** — nothing has been
measured against Kyle's scope yet. Verify each VERIFY-LIVE item on first real
connection and record raw responses into `tests/fixtures/`.

## Base

- Base URL: `http://192.168.100.1` (scope is DHCP server on its own AP,
  `UNI-xxxx` / `eVscope-xxxx`). `--ip` override existed in prior art for a scope
  joined to a home LAN — suggests station mode may exist (§9 Q4, VERIFY-LIVE).
- No auth. Plain HTTP.
- All models (eVscope 1/2, eQuinox 1/2, Odyssey/Pro) run the same `evsoft`
  firmware and expose the same API.
- Prior art sent a browser-like `User-Agent` (Chrome on Windows) on every
  session. Unknown whether the backend actually cares — VERIFY-LIVE (try honest
  `scopepull/x.y` UA first; fall back to browser UA if behavior differs).
- `GET /` returns 200 with the Vue app HTML — usable as a reachability check.
  DDD-disabled behavior (spec §2): not handled in prior art; unknown whether it
  404s, connection-refuses, or serves the app with an empty API. VERIFY-LIVE —
  this drives `doctor`'s "DDD disabled" message.

## Endpoints

### `GET /api/observations/list`

Returns the catalog of stored observations. Slow — prior art allowed **120 s**
read timeout and 3 attempts.

Response quirks:
- Body can contain bare `NaN` tokens → invalid JSON. Prior art did
  `text.replace("NaN", "null")` before parsing. Keep that (crude but works;
  fixture-test it).
- Normally a JSON **list** of observation objects. Prior art defensively also
  accepted a dict wrapper (`observations`/`obs`/`data`/`list`/`items` key) and
  a single bare object with `vpath`. Treat the plain list as the real shape
  until fixtures say otherwise.
- Content-Type `text/html` + `<html` body = you're getting the SPA, not the
  API (wrong path or DDD off).

Observation object fields used by prior art (full shape VERIFY-LIVE — record a
real response as a fixture):

| field | type | meaning |
|---|---|---|
| `vpath` | str | opaque path id — the download key, may contain `/` |
| `name` | str | observation name |
| `purpose` | str | e.g. science purpose label |
| `pmode` | str | mode (Enhanced Vision, etc.) |
| `obs_start` | int | epoch **milliseconds**, UTC |
| `nb_frames` | int | frame count |
| `obs_attr.tag_sc` | str | target tag (e.g. catalog id) |

Unknowns for `catalog.py` (§9 Q2): size estimate field? target display name vs
`tag_sc`? formats-available field? VERIFY-LIVE.

### `GET /api/observations/zip/{format}/0x0/{vpath}`

Streams a zip of one or more observations. `format` ∈ `fits|tiff|png`.
`vpath` is URL-encoded but with `/` kept (`quote(vpath, safe="/")`). Multiple
observations = the web UI concatenates vpaths into one job somehow — prior art
only ever requested **one observation per job**, and that is also our retry
granularity, so scopepull does the same (§9 Q1: per-observation downloads work
natively; batch-of-many syntax not reverse-engineered, not needed).

- `0x0` segment: presumed image-resize parameter (`WxH`, `0x0` = native/full).
  Never varied in prior art. Leave hardcoded; VERIFY-LIVE if curious.
- **THE GATE:** the backend will not stream unless a client is concurrently
  long-polling `GET /api/event`. Without it the request hangs then returns
  **502**. A 502 mid-session also means "backend not ready" → retry.
- Content-Type `text/html` on this endpoint = endpoint not found (old
  firmware?) — fail, don't retry.
- `Content-Length` is present at least sometimes — use for progress, don't
  rely on it.
- Zip contents: frame files + `manifest.json`. **A structurally valid zip
  containing only `manifest.json` is a failed export** (see FITS bug below) —
  must be detected and treated as failure, not archived.
- manifest.json contents: unknown — VERIFY-LIVE (§9 Q2: may carry the
  per-observation metadata we want for `observation.json`).

### `GET /api/event`

Long-poll event channel; **the required concurrent poll** during any zip pull.

- Odyssey holds the connection the full ~30 s when idle → first poll should
  use a short (~5 s) read timeout just to confirm the backend saw us; a read
  timeout still counts as "connected".
- Responses are JSON messages:
  - `{"cmd": "download", "status": ..., "progress": <int>, "nb_frames": <int>}`
    — server-side export progress (frames processed, not bytes).
  - `{"cmd": "obslist", ...}` — catalog change notification (ignorable for
    pulls; possibly useful someday for cache invalidation).
  - Other cmds unknown — VERIFY-LIVE, log unrecognized ones.
- Odyssey timing quirk (prior art's hardest-won lesson): being connected once
  is not enough; the backend wants the poller **actively re-polling** before
  the zip request. Prior art waited for first-poll ack + a ~3 s settle (grown
  on retries). Our `event_pump()` context manager must expose a "ready"
  awaitable that means *second poll cycle started*, not just "first response
  received".

### `POST /api/event`

Command channel. Only known command:

```json
{"cmd": "cancelDownload"}
```

Cancels the server-side export job. Prior art sends it before **every** pull
attempt (stale-job clearing — a stuck job otherwise blocks new ones) and on
user interrupt. `scopepull cancel` = exactly this. Response body/shape unknown
— VERIFY-LIVE. Other commands (delete? §9 Q3) unknown — the web UI has delete,
so capture a HAR of a delete click on the real scope before building
`--delete-after`.

## ⚠️ The FITS-on-Odyssey firmware bug (biggest open risk)

Prior art's final commit (2026-03, "Odyssey compatibility", tested on an
Odyssey IMX415):

> Odyssey returns manifest-only zips for FITS downloads because the firmware
> can't handle on-the-fly FITS conversion for large frames. […] FITS is a
> firmware bug (confirmed broken in web UI too). eQuinox 2 unaffected. PNG
> downloads work (43+ MB tested).

If still true on Kyle's Odyssey Pro + current firmware, **the spec's "FITS
only" goal is blocked at the scope side**, and options are: (a) newer firmware
fixed it — test first; (b) per-frame or smaller-batch FITS requests sneak under
the limit — probe; (c) fall back to TIFF (lossless) with a loud warning.
**Do not build Phase 2 transfer polish before running this live test.** The
detection logic (zip-has-frames validation, N-strikes → format advice) must be
ported regardless.

## Reliability contract distilled from prior art

Sequence per observation (their battle-tested order):

1. `POST /api/event {"cmd":"cancelDownload"}`, sleep ~1 s  (clear stale job)
2. Start event poller; await readiness (first-poll ack, escalating patience)
3. Settle delay ~3 s (escalating to ~18 s after manifest-only failures)
4. `GET .../zip/{fmt}/0x0/{vpath}` streaming, 256 KB chunks,
   connect 15 s / read 300 s
5. Validate: size > 0, `PK` magic, **zipfile CRC + contains ≥1 non-manifest
   file**
6. On drop (`ConnectionError`/`ReadTimeout`/`ChunkedEncodingError`): send
   cancel, stop poller, delete partial, back off 5 s, goto 1. Max ~10 attempts.
7. On 502: back off and retry (backend not ready).

Their "already downloaded" check was zip-file-exists; ours is the manifest DB
(spec §3) — stronger, keep theirs only as a sanity layer during ingest.

## Fixtures still needed from the real scope (Phase 0 completion checklist)

- [ ] `observations/list` raw body (incl. any NaN) → `tests/fixtures/`
- [ ] `/api/event` idle + during-download message sequences
- [ ] zip: one small real observation (FITS if it works, else PNG) + its
      `manifest.json`
- [ ] DDD-disabled behavior (toggle off in phone app, hit the API, record)
- [ ] HAR of web-UI delete click (for §9 Q3)
- [ ] FITS header dump of a few real frames (§9 Q5: Bayer keyword, exposure,
      gain, timestamps)
- [ ] Firmware version string if any endpoint exposes it (check HAR for a
      status/info endpoint the Vue app calls on load)

## ⚠️ Interrupted downloads can DESTROY observations on the scope (Kyle, field report)

Kyle has hit this himself using the web UI: start a big download, connection
drops mid-transfer, and the scope "dorks up" — the stacks can be **lost from
the scope**. This is worse than the retry-cost model the spec assumed. Design
consequences (binding):

1. **Snapshot the catalog before any pull.** Every run archives the raw
   `observations/list` response (timestamped, in the archive root under
   `_catalog/`) *before* the first zip request — if the scope eats data we at
   least know exactly what existed.
2. **Cancel aggressively and always.** `cancelDownload` on every abort path —
   interrupt, exception, pump death, SIGTERM. Never leave a server-side job
   half-alive; the stale job is the suspected corruption vector.
3. **Prefer many small jobs over one big one.** One observation per job is
   already the plan; consider pulling smallest-first on the initial sync so a
   flaky link fails on cheap observations, not the 8,000-frame science run.
4. **Preflight before touching the zip endpoint** (`doctor` logic inlined into
   `pull`): reachability, link quality (ping spread), free disk ≥ estimated
   size × 1.5, DDD enabled. Refuse to start a pull we can't plausibly finish.
5. **`--delete-after` stays off by default** and requires verified ingest +
   explicit confirm. (Already spec'd; reaffirmed given scope fragility.)
6. Warn loudly in README: don't operate the scope, don't let the host sleep
   mid-pull (OS sleep = dropped TCP = the bad case). Consider inhibiting
   sleep during transfers (systemd-inhibit / SetThreadExecutionState) — Phase 2.

## Wi-Fi availability model (researched 2026-08-30, help.unistellar.com)

- SSID is **`Odyssey-XXXX`** on Odyssey/Odyssey Pro (`UNI-xxxx`/`eVscope-xxxx`
  are the older models). **Open network** by default (password optional,
  user-set).
- **AP-only. No station mode** — the scope never joins a home network; the
  help center is explicit that it always creates its own network. §9 Q4 is
  answered: **NO** — unattended mode requires a host (or bridge device) within
  radio range of the scope. Prior art's `--ip` flag doesn't imply station mode.
- **Range ≈ 10 m** (Unistellar's own number; users report ~30 ft unobstructed).
  A desktop across the house will simply never see the AP.
- Web UI behaviors confirmed by the DDD help article: delete + delete-all
  exist in the UI (§9 Q3 — endpoint still needs a HAR); the scope **saves your
  download selection server-side** (you can disconnect the phone); Unistellar
  recommends not operating the scope during a download.
- Known firmware issue (changelog): "no network emitted" bug was fixed in a
  recent firmware — if the AP is invisible even up close, update firmware.
- Range extension that works in the field: a Wi-Fi repeater joined to the
  scope's AP (e.g. user report: ASUS RP-AC53, scope ~10 m from repeater,
  through two walls). For Kyle's setup, a client-bridge near the scope that
  backhauls to the Ubiquiti LAN would let the desktop pull without moving.

## Live findings — Odyssey Pro, Kyle's scope (2026-08-30, first contact)

Server: nginx/1.22.1. Listing: 15 observations, 14,959 bytes in 0.4 s — fast,
and **no bare NaN** on this firmware (keep the NaN guard anyway).

**Full observation field set** (§9 Q2 answered; real fixture pending scrub):
`alt, darkframe_timestamp, dec, depth, expo, gain, lat, long, name,
nameTarget, nb_frames, object_id, obsId, obs_attr, obs_end, obs_start,
obs_timestamp, period, pmode, pn, purpose, ra, resx, resy, sensor, sn, softh,
softv, type, uid_target, uuid, vpath`
— i.e. the catalog itself carries target display name (`nameTarget`),
coordinates (`ra/dec/alt`), exposure/gain, sensor id, resolution
(`resx/resy`), end time, and identifiers galore. `sn` is the SCOPE SERIAL —
scrub from fixtures (spec §8a). vpath shape: `prod/<uuid-v1>`.

**Event channel**: first GET answers immediately with
`{"cmd":"obslist","status":"updated","disk":{"total":...,"avail":...}}` —
the scope reports its DISK USAGE here. `doctor`/`status` should surface it
(Kyle's scope: 56.4 GB total, 22.8 MB avail = FULL). Subsequent idle polls
long-poll (held >8 s).

**`POST cancelDownload` HANGS when there is no job to cancel** — the scope
holds the connection instead of answering. Treat every cancel as
fire-and-forget: short timeout, swallow TransportError (client.py already
does; recon script fixed). Prior art's `timeout=(3,5)` + bare except was
load-bearing, not paranoia.

## ⚠️ FITS bug CONFIRMED on Kyle's Odyssey Pro (2026-09-01, firmware current as of that date)

MEASURED, live: `zip/fits` on a 2-frame observation → valid zip, manifest.json
only, ZERO frames. `zip/png` on the same observation → 2 frames, 2.8 MB,
~1.9 MB/s. So the failure is per-frame (sensor frame size), not observation
size, and it is NOT fixed by current firmware. TIFF untested as of that run —
recon script now tests fits/tiff/png.

More live facts:
- Zip layout: `<obsTimestamp>_000/` directory containing `manifest.json` and
  `<frameTimestamp>_StackInput.<ext>` frames (per-frame capture timestamps in
  the names).
- Zip stream has NO Content-Length (chunked) — byte-based progress only, plus
  frame progress from the event channel.
- Event during a completed small download: `{"cmd":"download","status":
  "ended","progress":0,"nb_frames":0}` — progress fields can be zero/late on
  tiny pulls; don't trust them for completion detection, trust the stream end
  + zip validation.
- `softh`/`softv` in the catalog listing likely = firmware/hardware versions —
  read them from the captured fixture to pin the firmware this was measured on.

Direction if TIFF works: pull TIFF (lossless), convert to FITS locally during
ingest (astropy), stamping headers from manifest.json + catalog fields
(ra/dec/expo/gain/obs timestamps). "FITS on disk" stays the deliverable; the
scope just can't be the thing that produces it.

## FORMAT DECISION — TIFF (2026-09-02, second live run)

Same 2-frame observation, back-to-back verdicts across two runs:

| format | run 1 (2026-09-01) | run 2 (2026-09-02) |
|---|---|---|
| FITS | manifest-only ✗ | manifest-only ✗ |
| TIFF | (not tested)     | **HAS FRAMES ✓** (2.6 MB, .tiff) |
| PNG  | HAS FRAMES ✓     | **manifest-only ✗** |

Two conclusions:
1. **TIFF is the pull format.** Lossless, carries the full 12-bit sensor data,
   and worked. FITS is broken on fw 4.2; PNG is unreliable.
2. **Any format can intermittently return an empty (manifest-only) zip** — PNG
   flipped from working to empty between runs on the SAME observation. Cause is
   almost certainly scope-side load / near-full disk (20 MB free). This is why
   zip-has-real-frames validation + observation-granularity retry are
   load-bearing, not optional. A downloader that trusts HTTP 200 ships empties.

Pipeline: pull TIFF → validate frames → during ingest, debayer + wrap to
12-bit FITS locally (astropy), headers from manifest.json:
`type=BAYER_GBRG`, `depth=12`, `expo` (µs), `gain`, `ra`/`dec`, `resx`/`resy`,
`obs_start/end`, per-frame timestamp from the filename. "FITS on disk" survives
as the deliverable; the scope just isn't the thing that makes it.

OPEN: confirm the scope's TIFF bit depth (expect 16-bit container holding
12-bit data). Drives whether we store as-is or repack to 12/16-bit FITS.

## Empty-export signature + retry requirement (2026-09-02)

MEASURED: a failed/not-ready export returns an **instant (~0.0s) valid-but-empty
zip**: bytes = `PK\x05\x06` (End Of Central Directory, zero entries) + zero
padding to exactly **10240 bytes**. Two empty variants seen: this fully-empty
zip, and a "manifest-only" zip (folder + manifest.json, no frames). Both must
be treated as FAILURE.

MEASURED: freeing scope disk (20MB -> plenty) did NOT fix EnhancedVision empty
exports — so it is NOT primarily a disk-staging problem. The Jupiter (PlanetEV,
2-frame, cropped) export succeeds on the first attempt; EnhancedVision exports
return the instant-empty zip and require the prior-art retry strategy: cancel
stale job -> pump actively re-polling -> escalating warm-up -> retry (up to
~8-10). This is the core of transfer.py, not optional polish. Detection: reject
`len<20000 and data[:4]==b"PK\x05\x06"`, and reject zips with zero non-manifest
entries, then retry.

## ✅ CORRECTION + COMPLETE PICTURE — deep-sky frames ARE raw Bayer (2026-09-02)

An earlier note here concluded the delivered frames are "half-res, debayered
mono, spec premise wrong." That was based ONLY on a PlanetEV (Jupiter)
observation and is WRONG for deep-sky. Corrected by a real EnhancedVision pull
(M101, measured):

- PlanetEV (Jupiter) StackInput: 2x2 phase spread 0.2% -> genuinely debayered
  mono. Planetary mode pre-processes. Special case.
- EnhancedVision (M101) StackInput: **2x2 phase spread 22.8% -> RAW GBRG BAYER
  MOSAIC.** 16-bit, 1452x1094, values to 65520. This is the real science data,
  and the spec's original raw-Bayer/debayer-with-Siril premise HOLDS for the
  deep-sky targets that matter.

Delivered resolution is 1452x1094 (half the catalog's stated 2904x2192 on both
axes) but genuinely raw Bayer — suitable for dark-subtract -> debayer -> stack.
Cause of the halving not established; does not block the pipeline.

Each EnhancedVision observation zip contains a FULL CALIBRATION SET:
- N x `<ts>_StackInput.tiff`  — raw GBRG Bayer light frames (16-bit)
- 1 x `<ts>_DarkframeMean.tiff` — master dark, same geometry (Bayer; low phase
  spread only because a dark carries no color signal)
- 1 x `<ts>_StackSum.tiff` — the scope's OWN stacked+debayered result
  (1452x1088, mono, full 16-bit range) — the reference to diff against
- `preview.jpg` — quick-look
- `manifest.json` — full per-observation metadata

Ingest plan (settled): unpack zip; StackInput -> per-frame FITS (uint16, tag
BAYER_GBRG, headers expo/gain/ra/dec/timestamps from manifest); keep Dark as a
calibration FITS; keep StackSum + preview as reference. Siril hook:
dark-subtract -> debayer GBRG -> register -> stack -> user's own stack.fit +
preview.png, for comparison against the scope's StackSum. "Deep Dark ate my
nebula?" is answered by that diff.

Empty-export retry (from the same run) VALIDATED LIVE: M101 attempts 1-3
returned the instant empty zip; attempt 4 (18s warm-up) returned 15.3 MB / 5
frames. The escalating warm-up + cancel + re-poll loop is confirmed necessary
and sufficient.

## ✅ THE EXPORT PROTOCOL — cracked from HAR captures (2026-09-03)

Two browser HAR captures (one 19-min large build, one 6-frame success) reveal
the actual export lifecycle over GET /api/event. Our whole retry approach was
wrong; this is the real contract.

The scope builds the zip frame-by-frame on the server and reports it over the
event long-poll:

    {"cmd":"download","status":"started","progress":1,"nb_frames":6}
    {"cmd":"download","status":"started","progress":2,"nb_frames":6}
    ... progress climbs 1 -> nb_frames, ~1 frame/sec ...
    {"cmd":"download","status":"ended","progress":0,"nb_frames":0}   <-- DONE

- **Completion signal: `status == "ended"`** (progress/nb_frames reset to 0).
  Confirmed in two independent captures.
- **Build time scales with frame count**: ~1 frame/sec. A 1082-frame export ran
  19 minutes and still wasn't done. Big observations take 15-30 min server-side.
- The browser triggers the build once, then just **polls patiently until
  `ended`** — it NEVER cancels mid-build.

### Our three bugs (all now explained and fixed)
1. Gave up in ~3 min; real builds need 15-30 min.
2. Sent `cancelDownload` before every retry — ABORTING the in-progress build.
   (M101 6-frame only worked because it rebuilt inside one warm-up window.)
3. Fired the zip GET immediately instead of waiting for `status=="ended"`.
   The instant ~10KB empty zip = "build not finished."

### Correct algorithm (implemented in transfer.pull v2)
1. `cancelDownload` ONCE to clear any stale prior job.
2. Trigger the build (issue the zip GET; returns the empty zip immediately for
   a large export — that's fine, the build has started server-side).
3. Keep ONE event pump polling; watch for `status=="started"` (build underway),
   report `progress/nb_frames`, then wait for `status=="ended"`. NEVER cancel.
   Timeout scales with nb_frames (~nb_frames*3s, floor minutes, generous cap).
4. On `ended`, GET the zip again -> now it streams the real archive.
5. Validate frames, ingest. Cancel only on genuine abort/failure.

App bundle: /assets/ui-DelGOngg.js (Vue). Event poller is fn `N` (setTimeout
recursion); download initiated by `kr`/`So`. Fetch it to confirm trigger + the
exact ended handling if any edge case appears.

## ✅✅ PROTOCOL FINALIZED — single held GET (2026-09-09, proven live on hardware)

Read directly from the Vue app's own code (`/assets/index-*.js`) plus live
probes against Kyle's scope. Supersedes every earlier "trigger then re-GET"
and "cancel-and-retry" model in this doc.

The app's download function is literally:

    _1 = (fmt, w, h, vpaths) => d1(`/api/observations/zip/${fmt}/${w}x${h}/${vpaths.join("|")}`)
    d1 = url => { const a = document.createElement("a"); a.href = url;
                  a.download = "unistellar-observation.zip"; a.click(); }

i.e. a plain anchor-navigation **GET** to the zip URL. Called as
`_1(format, 0, 0, [vpath, ...])` — so `0x0` (native size) is correct, and
multiple observations are joined with `|` (we pull one per job).

**That single GET is held open by the scope: it triggers the server-side build
AND streams the finished archive on the same connection.** No body bytes flow
while it builds (minutes for big observations); then the whole zip streams.
The `/api/event` channel reports `status:"started" progress:1..nb_frames` then
`status:"ended"` purely for the progress bar — you do NOT poll it to decide when
to download; the GET itself delivers the bytes.

MEASURED live, same 6-frame M101 observation:
  * GET **without** a preceding cancel  -> builds, streams full 15,267,840 B in 7.4 s ✓
  * GET **with** a `cancelDownload` just before -> instant 10,240 B empty zip ✗

So the two rules in transfer.py:
  1. **Never cancelDownload before the GET.** A pre-cancel suppresses the build.
     Cancel is only for cleaning up AFTER an aborted/failed transfer.
  2. **A concurrent event poll must be in flight** (the gate) — start the pump,
     let it issue one poll (~2.5 s), then GET. Don't wait for a poll *response*
     (an idle poll long-holds ~30 s).
  3. **read_timeout must exceed the whole build** (no bytes during build) —
     scaled to frame count.

End-to-end result (Skippy dual-homed, ethernet internet + wlo1 on the scope):
`scopepull pull` pulled M101 -> 15 MB -> ingested 2 raw GBRG Bayer frames +
FITS (BAYERPAT=GBRG, EXPTIME=4s, GAIN=321, RA/DEC, 22.7% Bayer phase spread
confirming intact mosaic). The tool works.
