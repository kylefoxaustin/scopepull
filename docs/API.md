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
