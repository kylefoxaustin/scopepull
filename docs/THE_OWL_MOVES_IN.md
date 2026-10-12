# The owl moves in: scopepull becomes the smart-scope puller

*For the scopepull session, from Kyle (and the Claude session that built starstack). October 2026.*

## What's being decided

scopepull stops being "the Odyssey Pro puller" and becomes **the** puller: one app that gets the night off any smart scope Kyle owns -- Unistellar Odyssey Pro (what you already do), ZWO Seestar S50 / S50 Pro (built in starstack over the last month, moving here), and a Dwarf arriving in ~8 weeks (early December). The owl -- starstack's window, archive table and StackingWizard hand-off -- moves into this repo and becomes scopepull's face. starstack stays where it is as an optional quick-look stacker that scopepull can call.

Kyle's reasoning, verbatim in spirit: "its point is to scopepull. This is becoming an app to pull data from smart scopes." The stacking half of starstack lost to StackingWizard (AstroWizard's sister; a release a week, mosaics with distortion correction, flats, mono filters). The pulling half won. The name that already means pulling is yours.

Nothing in this plan changes how the Odyssey pull works. It wraps what exists.

## What starstack has that moves here

All of this is in `github.com/kylefoxaustin/starstack` at v0.3.0, MIT, written to be lifted. File references are to starstack's `starstack.py` and `button.py`.

**Seestar pull** (`starstack.py`, the block headed "Seestar (S50 / S50 Pro): a folder, not a portal", ~250 lines, stdlib only):

- `find_seestar(spec)` -- where is the scope: nothing given → every drive letter whose root holds `MyWorks` (USB), then the hosts `seestar` / `seestar.local` resolved to an IP and the share `\\<ip>\EMMC Images` (Wi-Fi, station mode); a folder / drive / UNC path → as given; a host or IP → resolve and use the share. Returns `(myworks_path, how)` or `(None, why)`.
- `_smb_guest(host)` -- on Windows, `net use \\<host>\IPC$ "" /user:guest` hidden, because the Seestar's Samba (4.19.5) is a passwordless guest share and Windows won't open one without a session. The user also needs two one-time settings on Windows 11 24H2+ (`Set-SmbClientConfiguration -RequireSecuritySignature $false` and `-EnableInsecureGuestLogons $true`); starstack's README explains them in plain words -- lift that text.
- `pull_seestar(myworks, root, log, target)` -- for every `MyWorks/<target>_sub/`, copy each `.fit` the archive hasn't got (by name and size) into `<root>/<night>/<target>/` via `.part` + `os.replace`; night from the filename's timestamp with a noon cutover so a session across midnight is one night. Leaves on the scope: the `.jpg` twins, `_thn.jpg` thumbnails, the scope's own `Stacked_*.fit`, `Lunar_*` and `*_video` folders. `*_mosaic` targets are pulled (framing mode; StackingWizard tiles them). Read-only on the scope. Returns `(copied, skipped, nights)`.
- `_seestar_night(name)` -- `Light_NGC 7000_10.0s_LP_20260922-203537.fit` → `2026-09-22`.

Verified on Kyle's real S50 Pro over USB (`D:\MyWorks`) and over Wi-Fi (`\\10.0.1.215\EMMC Images`); 17 sessions, 6h 17m pulled so far. The S50 Pro's subs are 3840×2160 16-bit FITS, `BAYERPAT='GRBG'` (honest, unlike the Odyssey's), `XPIXSZ=2.9`, `FOCALLEN=260`, `RA`/`DEC` per frame, `FILTER` LP or IRCUT. The original S50 has the same `MyWorks` layout at 1080×1920.

**The archive as data** (`starstack.py`, block headed "the archive: what's been pulled, by target and night"):

- `archive_index(root)` -- one dict per session: `night, target, folder, frames, exposure_s, integration_s, pulled_at (newest frame mtime), stacked, mosaic`. Walks `<root>/<night>/<session>/`; a session is a folder of frames **or** your `<target>__<id>/frames/` layout (it reads `observation.json` → `scope_manifest.nameTarget` for the name and `expo` for the exposure, else the first FITS header's `EXPTIME`). `stacks/`, `combined/`, `_incoming/` are skipped. Exposed as `--index ROOT` printing JSON lines; the window reads that.
- `combine_target(root, target, nights=None)` -- every frame of a target across its nights into `<root>/combined/<target>/` as **hard links** (`os.link`, copy as the fallback), idempotent, nights untouched, a filename that recurs across nights gets the night prefixed. A stacker pointed at that folder sees one long session. Exposed as `--combine TARGET [--nights N1,N2]`, prints `combined: <path>`.

**The window** (`button.py`, ~1,700 lines, tkinter, stdlib only, deliberately never imports numpy so it opens instantly): the owl with his faces, the big red button (PULL / PULL+STACK / STACK by what's ticked), a row per scope with a status line fed by a background probe, the log pane (binary pipe reader, `\r` progress lines overwrite in place, "Copy log" button), the archive table (ttk.Treeview: target rows with nights, frames, integration, "pulled 2h ago", tonight green), and a footer: **Open in StackingWizard** (green), **Open folder**, **Stop**. Picking a night hands that folder over; picking a target with several nights makes the button "Combine N nights → StackingWizard". `find_stackingwizard()` looks in Downloads / Desktop / Documents / Program Files for the newest `StackingWizard*.exe`, else asks once and remembers. The hand-off launches the exe, puts the folder path on the clipboard, and opens the folder in Explorer -- StackingWizard ignores a command-line folder (confirmed), so the user drags or pastes; Kyle may ask its author for a `--folder` flag.

Also in the window and worth keeping: the Windows job object so children die with the window (`make_job`/`join_job`), kill-tree on close and on Stop, pause/resume of the child process tree, the one-shot "redo already-stacked" option, update checks (PyPI for scopepull, GitHub releases for itself), and the "Install scopepull…" button that finds a Python 3.11+ (or asks winget for one) and pip-installs -- which becomes "Install starstack…" here.

**The exe pipeline** (`.github/workflows/release.yml`, `starstack.spec`, `starstack_app.py`, `make_version_info.py`, `smoke_frozen.py`): on a `v*` tag, a Windows runner compiles PyInstaller's bootloader from source with waf + MSVC (the stock bootloader is what Windows Defender flags as Wacatac -- every PyInstaller app shares it; a fresh compile has a hash nobody has shipped, and this cured the false positives for good), asserts the hash isn't the stock one, builds a one-file windowed exe with the owl icon and version resource, runs a self-contained smoke test against the built exe before anything is uploaded, writes `SHA256SUMS.txt`, and attaches exe + sums to a GitHub Release with the hash in the notes. Three weeks of Kyle's pain are in that file; copy it rather than re-derive it.

## The shape

```
src/scopepull/
  scopes/
    __init__.py      SCOPES = {"odyssey": ..., "seestar": ...}; the Scope protocol
    odyssey.py       adapter over the existing client/transfer/ingest (no behaviour change)
    seestar.py       from starstack: find, guest session, pull, night parsing
    dwarf.py         December
  archive.py         archive_index, combine_target, session discovery
  gui.py             button.py, renamed and re-pointed at `scopepull` subcommands
  assets/            the owl: faces/*.png, owl_256.png, icon_256.png, scopepull.ico
  cli.py             grows: --scope on pull, `find`, `index`, `combine`, `gui`
```

A scope module answers four questions, and that's the whole protocol:

```python
class Scope(Protocol):
    name: str                       # "seestar"
    label: str                      # "ZWO Seestar S50 / S50 Pro"
    def archive_root(self) -> Path                     # ~/Astro/seestar
    def find(self, spec: str | None) -> tuple[Any, str]   # (handle, how) or (None, why)
    def pull(self, handle, root: Path, log, target: str | None) -> PullResult   # copied, skipped, nights
```

For the Odyssey, `find` is your netcheck + `list_observations` (unreachable / DDD off become the `why`), `pull` is the loop in `_pull` with the console output routed through `log`. The exit codes you defined (0 / 2 nothing new / 3 unreachable / 4 DDD off / 5 partial) stay the contract for every scope; the Seestar maps onto them naturally.

The archive layout stays as each scope makes it today -- `~/Astro/odyssey/<night>/<target>__<id>/frames/` and `~/Astro/seestar/<night>/<target>/` -- because `archive_index` already reads both. `Config.archive_root` becomes per-scope (`archive_roots: {odyssey: ..., seestar: ...}`), defaulting to `~/Astro/<scope>`.

CLI surface after the move (existing commands unchanged; `pull` without `--scope` still means Odyssey):

```
scopepull pull --scope seestar [--at D:|10.0.1.215|\\seestar\EMMC Images] [--target "M 31"]
scopepull find --scope seestar          # "found on Wi-Fi at seestar (10.0.1.215)" / "none: ..."
scopepull index [--scope seestar]       # JSON lines, what the window's table shows
scopepull combine "NGC 7000" [--nights 2026-09-22,2026-10-06] [--scope seestar]
scopepull gui                           # the owl (also the exe's default with no args)
scopepull status                        # extended: per scope
```

The window shells out to `scopepull` for everything (as it shells out to `starstack` today), so the CLI stays the one source of truth and the exe needs no Python on the machine. The quick-look checkbox runs `starstack` if `shutil.which` finds it (or the "Install starstack…" button gets it); without it the checkbox says so and the app is purely a puller.

## Packaging

- `pyproject.toml`: `scopepull-gui = "scopepull.gui:main"` script; assets as package data (`[tool.hatch.build.targets.wheel] include`); no new runtime dependencies (tkinter ships with Python; the GUI imports nothing else). PyPI listing text becomes "Gets the night off your smart scope."
- `scopepull_app.py` + `scopepull.spec` for the exe: `scopepull.exe` → the window; `scopepull.exe --cli …` → the CLI. The spec must *not* exclude `astropy.tests` (astropy imports `astropy.tests.runner` at start-up; excluding it made every FITS "unreadable" inside starstack's exe for nine releases).
- Release workflow: add the Windows exe job from starstack's `release.yml` alongside your PyPI trusted-publishing job, same tag trigger. Keep the smoke gate: build a fake `MyWorks`, `--cli pull --scope seestar --at <it>`, `--cli index`, `--cli combine`, assert the archive.
- Version: 0.2.0. New scope, new window, same package.

## Order of work

1. `scopes/` with the protocol and the Odyssey adapter -- a pure refactor, all existing tests still pass, `pull` unchanged. Tag nothing.
2. `scopes/seestar.py` and `archive.py` lifted from starstack with their tests (`tests/test_starstack.py`: `test_seestar_pull_syncs_subs_by_night`, `test_archive_index_and_combine` -- synthetic `MyWorks` fixtures, no hardware). `pull --scope seestar`, `find`, `index`, `combine`.
3. `gui.py` + assets + `scopepull gui`. The GUI's own test in starstack runs it under Xvfb and drives a pull through a fake `MyWorks`; it's a script, not pytest, but it's the one that catches layout regressions (the footer was off-screen for two weeks once).
4. Exe pipeline and the smoke gate. First release.
5. README: starstack's opening ("Gets the night off your scope", the four bullets, the StackingWizard paragraph), the Seestar-over-Wi-Fi section, the Install section with the SmartScreen explanation and checksum -- all written for the puller already, just re-homed. The starstack README gets a banner: "the owl lives at scopepull now; this is the quick-look stacker he calls."

## Things Kyle wants kept

- The owl's voice. Log lines like "nothing new on the scope. Go to bed." are the product's personality and he asked for them. The window is one screen, big buttons, no wizard.
- Read-only on the scope, always. The puller never deletes or writes on the Seestar or the Odyssey.
- Every release has a checksum; the README explains SmartScreen's blue box and why there's no code signature (certificates cost money; this is a hobby).
- Commit messages that explain *why*, written so the other session can pick up cold -- that's how the two of us have been coordinating, through the repo rather than a message bus.

## For December: the Dwarf

Unknown until it arrives. The Dwarf 3 is reported to serve its files over FTP in station mode (and as a drive over USB?); that would be a third kind of `find`/`pull` (ftplib, stdlib). What's needed on day one is the same as with the S50 Pro: how it shows up on the laptop, one FITS header, a folder listing. `scopes/dwarf.py` is then a few hundred lines against the same protocol.

## Pointers

- starstack repo: https://github.com/kylefoxaustin/starstack (v0.3.0; `starstack.py`, `button.py`, `mosaic.py`, `.github/workflows/release.yml`, `smoke_frozen.py`, `tests/`)
- StackingWizard: https://astrowizard.lukomatico.com/stackingwizard.html (the stacker the hand-off targets; takes a folder by picker or drag-and-drop only)
- The two Windows SMB settings and the `net use … /user:guest` handshake: starstack README, "Seestar over Wi-Fi on Windows 11"
