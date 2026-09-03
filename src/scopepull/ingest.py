"""Transactional ingest: zip -> archive layout -> manifest commit.

An EnhancedVision export zip carries a full calibration set (docs/API.md):
raw GBRG Bayer light frames (StackInput), a master dark (DarkframeMean), the
scope's own stacked result (StackSum), a preview.jpg, and manifest.json.

Layout produced (spec §3):

    <root>/YYYY-MM-DD/<target-slug>__<id-short>/
        frames/*.tiff          raw Bayer lights, verbatim
        frames/*.fits          per-light FITS (uint16, BAYERPAT=GBRG + metadata)
        calibration/dark.tiff, dark.fits
        reference/stacksum.tiff, preview.jpg
        observation.json       manifest + pull metadata
        SHA256SUMS

Ingest is transactional: everything is built under <dir>.partial/, then
os.replace()'d into place; the manifest row is written only after the rename.
A crash leaves either nothing or a complete observation — never a half one.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from astropy.io import fits

from .catalog import Observation
from .config import Config
from .manifest import Manifest

# TIFF role classification by filename (case-insensitive substring).
_LIGHT = "stackinput"
_DARK = "darkframemean"
_STACKSUM = "stacksum"


@dataclass
class IngestResult:
    obs_id: str
    dir: Path
    light_count: int
    fits_written: int
    bytes: int


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _bayer_pattern(manifest: dict[str, Any]) -> str | None:
    """'BAYER_GBRG' -> 'GBRG'; anything else / debayered -> None."""
    t = str(manifest.get("type", ""))
    if t.startswith("BAYER_") and len(t) == len("BAYER_GBRG"):
        return t.split("_", 1)[1]
    return None


def _write_fits(
    tiff_path: Path, fits_path: Path, manifest: dict[str, Any], bayer: str | None
) -> None:
    """Wrap a 16-bit TIFF frame into FITS with astro headers from the manifest.

    Pixels are written as-is (no flip); BAYERPAT matches the TIFF orientation.
    If a later visual debayer shows swapped colors, the flip/pattern convention
    is the thing to revisit (noted in docs/API.md).
    """
    arr = tifffile.imread(tiff_path)
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    hdu = fits.PrimaryHDU(data=arr)
    h = hdu.header
    h["OBJECT"] = str(manifest.get("nameTarget", ""))[:68]
    h["INSTRUME"] = str(manifest.get("sensor", ""))[:68]
    h["TELESCOP"] = "Unistellar Odyssey"
    expo = manifest.get("expo")
    if isinstance(expo, (int, float)):
        h["EXPTIME"] = (expo / 1_000_000.0, "seconds (from manifest expo, us)")
    gain = manifest.get("gain")
    if isinstance(gain, (int, float)):
        h["GAIN"] = gain
    for key, mk in (("RA", "ra"), ("DEC", "dec")):
        v = manifest.get(mk)
        if isinstance(v, (int, float)):
            h[key] = (v, "degrees")
    depth = manifest.get("depth")
    if isinstance(depth, (int, float)):
        h["ADCBITS"] = (int(depth), "sensor ADC bit depth")
    if bayer:
        h["BAYERPAT"] = (bayer, "Bayer color filter array pattern")
        h["XBAYROFF"] = 0
        h["YBAYROFF"] = 0
    obs_start = manifest.get("obs_start")
    if isinstance(obs_start, (int, float)) and obs_start > 0:
        h["DATE-OBS"] = datetime.fromtimestamp(obs_start / 1000, tz=UTC).isoformat()
    hdu.writeto(fits_path, overwrite=True)


def ingest_zip(
    zip_path: Path,
    obs: Observation,
    cfg: Config,
    manifest_db: Manifest,
    *,
    write_fits: bool = True,
) -> IngestResult:
    """Unpack an observation zip into the archive and commit the manifest row."""
    obs_dir = cfg.archive_root / obs.date_str / obs.dir_name
    partial = obs_dir.with_name(obs_dir.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    (partial / "frames").mkdir(parents=True)
    (partial / "calibration").mkdir()
    (partial / "reference").mkdir()

    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if not n.endswith("/")]
        manifest: dict[str, Any] = {}
        for n in members:
            if n.endswith("manifest.json"):
                manifest = json.loads(z.read(n))
                break
        bayer = _bayer_pattern(manifest)

        light_tiffs: list[Path] = []
        for n in members:
            base = Path(n).name
            low = base.lower()
            if low.endswith("manifest.json"):
                continue
            if _LIGHT in low:
                out = partial / "frames" / base
            elif _DARK in low:
                out = partial / "calibration" / base
            elif _STACKSUM in low:
                out = partial / "reference" / base
            else:  # preview.jpg and anything else -> reference
                out = partial / "reference" / base
            out.write_bytes(z.read(n))
            if _LIGHT in low and low.endswith((".tif", ".tiff")):
                light_tiffs.append(out)

    # TIFF -> FITS for lights and the dark.
    fits_written = 0
    if write_fits:
        for t in light_tiffs:
            _write_fits(t, t.with_suffix(".fits"), manifest, bayer)
            fits_written += 1
        for dark in (partial / "calibration").glob("*.tif*"):
            _write_fits(dark, dark.with_suffix(".fits"), manifest, bayer)
            fits_written += 1

    # observation.json = scope manifest + our pull metadata.
    (partial / "observation.json").write_text(
        json.dumps(
            {
                "scope_manifest": manifest,
                "catalog": obs.raw,
                "pulled_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
                "bayer_pattern": bayer,
            },
            indent=2,
        )
    )

    # SHA256SUMS over everything, and collect for the manifest DB.
    files: list[tuple[str, str, int]] = []
    sums_lines: list[str] = []
    for p in sorted(partial.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            rel = p.relative_to(partial).as_posix()
            digest = _sha256(p)
            files.append((rel, digest, p.stat().st_size))
            sums_lines.append(f"{digest}  {rel}")
    (partial / "SHA256SUMS").write_text("\n".join(sums_lines) + "\n")

    # Atomic commit: rename dir into place, THEN write the manifest row.
    obs_dir.parent.mkdir(parents=True, exist_ok=True)
    if obs_dir.exists():
        shutil.rmtree(obs_dir)
    partial.replace(obs_dir)

    manifest_db.record(
        obs_id=obs.obs_id,
        target=obs.target,
        started_at=obs.started_at,
        frame_count=len(light_tiffs),
        format=cfg.format,
        dir=str(obs_dir.relative_to(cfg.archive_root)),
        files=files,
    )
    total = sum(sz for _, _, sz in files)
    return IngestResult(obs.obs_id, obs_dir, len(light_tiffs), fits_written, total)
