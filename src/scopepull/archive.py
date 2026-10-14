"""The archive as data: what's been pulled, by target and by night.

Every scope lands its nights in `<root>/<night>/<session>/`, but each one names
the session its own way:

    Odyssey   <root>/2026-01-31/m81-bode-s-galaxy__38102043/frames/*.fits
    Seestar   <root>/2026-09-22/NGC 7000/*.fit

so this module is what reads both and answers "what have I got?" in one shape.
`scopepull index` prints it as JSON lines; the window's archive table is that
JSON, and `combine` is how several nights of one target become a single folder
a stacker can swallow.

Lifted from starstack (archive_index / combine_target, v0.3.0) and re-pointed
at pathlib and real types. The semantics are unchanged on purpose: starstack's
own tests pin them, and the window was written against these exact keys.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .log import Log, quiet

FITS_EXT = {".fit", ".fits", ".fts"}
TIFF_EXT = {".tif", ".tiff"}
IMAGE_EXT = FITS_EXT | TIFF_EXT | {".png", ".jpg", ".jpeg"}

# Folders under the root that are output or scratch, never a night.
ARCHIVE_SKIP = {"stacks", "combined", "_incoming"}
# Frames that aren't subs: the scope's own stack, previews, thumbnails, darks.
# Anchored, not loose substrings. A Seestar puts the target name in every
# filename (Light_<target>_10.0s_...), so matching "final" or "stacked"
# anywhere would make a target called "M31 final" or "Master Ring" disappear
# from `index` and `combine` with no explanation.
EXTRA_PREFIXES = ("stacked_", "preview", "thumb", "master", "final_")
EXTRA_MARKERS = ("_thn.", "stacksum", "darkframemean")
# Where an observation keeps its lights, when it keeps them in a subfolder.
FRAME_DIRS = ("frames", "lights", "light")


@dataclass
class Session:
    """One target on one night: a folder of sub-frames.

    The field names are the window's column names and the JSON keys that
    `scopepull index` prints — starstack's button was written against them, so
    they don't get renamed lightly.
    """

    night: str
    target: str
    folder: str
    frames: int
    exposure_s: float
    integration_s: float
    pulled_at: float  # newest frame's mtime, epoch seconds
    stacked: bool
    mosaic: bool
    scope: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_extra(name: str) -> bool:
    """Is this file something other than a sub-frame? The scope's own finished
    stack, a preview, a thumbnail, a master dark."""
    low = name.lower()
    return low.startswith(EXTRA_PREFIXES) or any(m in low for m in EXTRA_MARKERS)


def has_images(folder: Path) -> bool:
    try:
        return any(p.suffix.lower() in IMAGE_EXT for p in folder.iterdir())
    except OSError:
        return False


def prefer_fits(paths: list[Path]) -> tuple[list[Path], int]:
    """scopepull writes every Odyssey frame twice: the scope's TIFF verbatim and
    a FITS of the same pixels with the headers filled in (BAYERPAT, EXPTIME,
    DATE-OBS). Counting both would count each frame twice, so when a TIFF and a
    FITS share a stem the FITS wins — it knows things the TIFF can't.
    Returns (kept, number of TIFFs set aside)."""
    by_stem: dict[str, set[str]] = {}
    for p in paths:
        by_stem.setdefault(str(p.with_suffix("")).lower(), set()).add(p.suffix.lower())
    kept, dropped = [], 0
    for p in paths:
        ext = p.suffix.lower()
        twins = by_stem[str(p.with_suffix("")).lower()]
        if ext in TIFF_EXT and twins & FITS_EXT:
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def read_manifest(folder: Path) -> dict[str, Any]:
    """The scope's own description of a session, if it left one. Unistellar:
    manifest.json next to the frames. scopepull: observation.json with that
    same manifest verbatim under "scope_manifest" (a key starstack reads too —
    docs/STARSTACK.md). {} if neither."""
    for name, key in (("manifest.json", None), ("observation.json", "scope_manifest")):
        p = folder / name
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mf = raw.get(key) if key else raw
        if isinstance(mf, dict):
            return dict(mf)
    return {}


def frame_dir(folder: Path) -> Path:
    """Where this session's lights are: frames/ if it has one, else the folder."""
    for name in FRAME_DIRS:
        d = folder / name
        if d.is_dir() and has_images(d):
            return d
    return folder


def session_frames(folder: Path) -> list[Path]:
    """The sub-frames of one session: FITS over TIFF twins, no JPEGs, no
    thumbnails, and not the scope's own finished stack."""
    d = frame_dir(folder)
    try:
        paths = sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)
    except OSError:
        return []
    paths, _ = prefer_fits(paths)
    return [
        p
        for p in paths
        if p.suffix.lower() not in {".jpg", ".jpeg", ".png"} and not is_extra(p.name)
    ]


def session_name(folder: Path) -> str:
    """Name a session by its target when the scope told us, else by the folder
    (and `M 31_sub` is the Seestar's name for `M 31`)."""
    name = str(read_manifest(folder).get("nameTarget") or "").strip()
    name = name or folder.name
    if name.lower().endswith("_sub"):
        name = name[:-4]
    return re.sub(r'[<>:"/\\|?*]+', "-", name).strip(" .") or "session"


def session_exposure(folder: Path, frames: list[Path]) -> float:
    """Seconds per sub: from the scope's manifest if there is one (Unistellar
    records `expo` in microseconds), else from the first frame's header."""
    expo = read_manifest(folder).get("expo")
    if expo is not None:
        try:
            return float(expo) / 1e6
        except (TypeError, ValueError):
            pass
    for p in frames[:1]:
        if p.suffix.lower() in FITS_EXT:
            # astropy is a second of import; only a Seestar night (no manifest)
            # ever gets this far, and then only for one frame of each session.
            try:
                from astropy.io import fits

                header = fits.getheader(p)
                return float(header.get("EXPTIME") or header.get("EXPOSURE") or 0)
            except Exception:
                return 0.0
    return 0.0


def _sessions_in_night(night_dir: Path) -> list[Path]:
    """The session folders of one night, newest layout or oldest: a folder of
    frames, or an observation folder with frames/ inside. `.partial` is a pull
    still in progress and isn't ours yet."""
    try:
        subs = sorted(p for p in night_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    out = []
    for d in subs:
        low = d.name.lower()
        if low.endswith(".partial") or low in ARCHIVE_SKIP:
            continue
        if has_images(d) or has_images(frame_dir(d)):
            out.append(d)
    if not out and has_images(night_dir):
        out = [night_dir]  # a plain folder of frames dropped into the archive
    return out


def archive_index(root: Path, scope: str = "") -> list[Session]:
    """Every session in an archive, newest night first."""
    out: list[Session] = []
    if not root.is_dir():
        return out
    try:
        nights = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name.lower() not in ARCHIVE_SKIP),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return out
    stacks = root / "stacks"
    for night_dir in nights:
        for sess in _sessions_in_night(night_dir):
            frames = session_frames(sess)
            if not frames:
                continue
            target = session_name(sess)
            expo = session_exposure(sess, frames)
            try:
                pulled = max(p.stat().st_mtime for p in frames)
            except OSError:
                pulled = 0.0
            out.append(
                Session(
                    night=night_dir.name,
                    target=target,
                    folder=str(sess),
                    frames=len(frames),
                    exposure_s=expo,
                    integration_s=expo * len(frames),
                    pulled_at=pulled,
                    stacked=(stacks / f"{target}.tif").exists(),
                    mosaic="mosaic" in sess.name.lower(),
                    scope=scope,
                )
            )
    return out


def combine_target(
    root: Path, target: str, nights: list[str] | None = None, log: Log | None = None
) -> Path | None:
    """Every frame of one target across its nights, in `<root>/combined/<target>/`,
    as hard links — no copies, no disk — with a copy as the fallback for drives
    that can't link. A stacker pointed at that folder sees one long session.
    The nights themselves are never touched. Returns the folder."""
    log = log or quiet()
    rows = [
        s for s in archive_index(root) if s.target == target and (not nights or s.night in nights)
    ]
    if not rows:
        log(f"no sessions of {target!r} in {root}.")
        return None
    dest = root / "combined" / target
    dest.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in dest.iterdir()}
    linked = copied = kept = 0
    for row in sorted(rows, key=lambda r: r.night):
        for p in session_frames(Path(row.folder)):
            name = p.name
            if name in have:
                # The same filename from another night (the Odyssey does this):
                # keep the first and prefix the rest with the night.
                existing = dest / name
                if existing.exists() and existing.stat().st_size == p.stat().st_size:
                    kept += 1
                    continue
                name = f"{row.night}_{name}"
                if name in have:
                    kept += 1
                    continue
            out = dest / name
            try:
                os.link(p, out)
                linked += 1
            except OSError:
                try:
                    shutil.copyfile(p, out)
                    copied += 1
                except OSError as e:
                    log(f"  {name}: {e}")
                    continue
            have.add(name)

    nights_s = ", ".join(sorted({r.night for r in rows}))
    mins = int(sum(r.integration_s for r in rows) // 60)
    log(
        f"combined {target}: {len(rows)} night{'s' if len(rows) != 1 else ''} ({nights_s}), "
        f"{sum(r.frames for r in rows)} frames, {mins // 60}h {mins % 60:02d}m  ->  {dest}"
    )
    if linked:
        log(
            f"  {linked} hard links (no extra disk)"
            + (f", {copied} copied" if copied else "")
            + (f", {kept} already there" if kept else "")
            + "."
        )
    elif copied:
        log(
            f"  {copied} copied (the archive isn't on a drive that can hard-link; "
            "this costs disk)" + (f", {kept} already there" if kept else "") + "."
        )
    elif kept:
        log(f"  all {kept} already there.")
    return dest


def summarize(sessions: list[Session]) -> str:
    """One line for `status`: '8 sessions, 3 nights, 1204 frames, 6h 17m'."""
    if not sessions:
        return "empty"
    mins = int(sum(s.integration_s for s in sessions) // 60)
    nights = len({s.night for s in sessions})
    frames = sum(s.frames for s in sessions)
    return (
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}, "
        f"{nights} night{'s' if nights != 1 else ''}, "
        f"{frames} frames, {mins // 60}h {mins % 60:02d}m"
    )
