"""Transactional ingest: zip -> archive layout -> manifest commit.

An EnhancedVision export zip carries a full calibration set (docs/API.md):
raw Bayer light frames (StackInput; RGGB as stored -- the sensor is GBRG but
the 2x-downsampled export is row-offset, see _measure_bayer_pattern), a master
dark (DarkframeMean), the scope's own stacked result (StackSum), a preview.jpg,
and manifest.json.

Layout produced (spec §3):

    <root>/YYYY-MM-DD/<target-slug>__<id-short>/
        frames/*.tiff          raw Bayer lights, verbatim
        frames/*.fits          per-light FITS (uint16, BAYERPAT measured from pixels + metadata)
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
import math
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
from .fsutil import replace_retry
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


def _sensor_bayer_pattern(manifest: dict[str, Any]) -> str | None:
    """'BAYER_GBRG' -> 'GBRG'; anything else / debayered -> None.

    NOTE: this is the pattern of the SENSOR (IMX415), as the scope reports it.
    It is NOT necessarily the pattern of the exported StackInput TIFF -- see
    `_measure_bayer_pattern` below. Use that for the BAYERPAT header.
    """
    t = str(manifest.get("type", ""))
    if t.startswith("BAYER_") and len(t) == len("BAYER_GBRG"):
        return t.split("_", 1)[1]
    return None


# The exported StackInput TIFF is a 2x downsample of the sensor (1452x1094 from
# 2904x2192) and its 2x2 phases do NOT line up with the sensor's: measured on
# real Odyssey Pro frames (M81, Feb 2026), the two green phases sit on the
# ANTI-diagonal -- (0,1) and (1,0) -- which is the RGGB/BGGR family, while the
# manifest says the sensor is GBRG (greens on the main diagonal). Unistellar's
# own help page ("About the RAW files we provide") says eVscope / eQuinox /
# Odyssey raw frames are RGGB, which agrees. A vertical one-row offset between
# sensor readout and export is the simplest explanation: flipping rows turns
# GBRG into RGGB.
#
# Writing BAYERPAT=GBRG for RGGB data feeds green pixels into both the red and
# blue channels of every debayer downstream (starstack, Siril, PixInsight):
# M81's warm core came out grey-magenta with GBRG and correctly yellow with
# RGGB (12-frame test: core R/B 1.45 with RGGB vs 1.07 with GBRG).
#
# So: measure the pattern from the pixels of the frame we are about to write,
# and only fall back to the manifest if the pixels don't say. Statistics can
# tell which diagonal holds the greens, but not R from B; within a family we
# pick the one Unistellar documents (RGGB) or the sensor's (GBRG).
_ROW_FLIP = {"RGGB": "GBRG", "GBRG": "RGGB", "BGGR": "GRBG", "GRBG": "BGGR"}


def _measure_bayer_pattern(arr: np.ndarray, sensor: str | None) -> str | None:
    """Which 2x2 pattern the pixels actually follow, or None if not a mosaic.

    Greens are the two phases that agree with each other; in a real mosaic the
    four phase means differ by 20-40 % (sky through R, G, B filters). A mono or
    already-debayered frame has four phases that agree, and gets None.
    """
    if arr.ndim != 2 or min(arr.shape) < 16:
        return None
    a = arr.astype(np.float64)
    a = np.minimum(a, np.percentile(a, 98))  # keep stars out of the statistics
    ph = {
        (0, 0): a[0::2, 0::2].mean(),
        (0, 1): a[0::2, 1::2].mean(),
        (1, 0): a[1::2, 0::2].mean(),
        (1, 1): a[1::2, 1::2].mean(),
    }
    vals = np.array(list(ph.values()))
    spread = vals.max() - vals.min()
    if vals.mean() <= 0 or spread / vals.mean() < 0.03:
        return None  # flat: mono / debayered (e.g. PlanetEV)
    d_main = abs(ph[(0, 0)] - ph[(1, 1)])
    d_anti = abs(ph[(0, 1)] - ph[(1, 0)])
    if min(d_main, d_anti) > 0.5 * spread:
        return None  # no matching pair: not a Bayer mosaic
    if d_anti <= d_main:
        # greens on the anti-diagonal: RGGB or BGGR. Unistellar documents RGGB.
        return "RGGB" if sensor not in ("BGGR",) else "BGGR"
    # greens on the main diagonal: GBRG or GRBG -- the sensor's own order.
    return sensor if sensor in ("GBRG", "GRBG") else "GBRG"


def _bayer_pattern(manifest: dict[str, Any], sample: np.ndarray | None = None) -> str | None:
    """The BAYERPAT to write: measured from `sample` when we have one, else the
    row-flipped sensor pattern (what the export is known to do), else None."""
    sensor = _sensor_bayer_pattern(manifest)
    if sample is not None:
        measured = _measure_bayer_pattern(sample, sensor)
        if measured:
            return measured
        if sensor:
            return None  # sensor says Bayer, pixels say mono: trust pixels
    return _ROW_FLIP.get(sensor) if sensor else None


def _num(v: object) -> float | None:
    """A finite number from the manifest, or None. The Odyssey writes NaN for
    ra/dec on an observation it never plate-solved (an aborted 14 MB Western
    Veil, live), and FITS headers cannot hold NaN -- astropy raises, and one
    bad manifest took a whole six-observation pull down. bool is excluded on
    purpose (it is an int in Python)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _write_fits(
    tiff_path: Path, fits_path: Path, manifest: dict[str, Any], bayer: str | None
) -> None:
    """Wrap a 16-bit TIFF frame into FITS with astro headers from the manifest.

    Pixels are written as-is (no flip). BAYERPAT is the pattern MEASURED from
    the exported pixels (RGGB on real Odyssey Pro frames), not the sensor's
    GBRG from the manifest -- see the note above `_measure_bayer_pattern`.
    """
    arr = tifffile.imread(tiff_path)
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    hdu = fits.PrimaryHDU(data=arr)
    h = hdu.header
    h["OBJECT"] = str(manifest.get("nameTarget", ""))[:68]
    h["INSTRUME"] = str(manifest.get("sensor", ""))[:68]
    h["TELESCOP"] = "Unistellar Odyssey"
    expo = _num(manifest.get("expo"))
    if expo is not None:
        h["EXPTIME"] = (expo / 1_000_000.0, "seconds (from manifest expo, us)")
    gain = _num(manifest.get("gain"))
    if gain is not None:
        h["GAIN"] = gain
    for key, mk in (("RA", "ra"), ("DEC", "dec")):
        v = _num(manifest.get(mk))
        if v is not None:
            h[key] = (v, "degrees")
    depth = _num(manifest.get("depth"))
    if depth is not None:
        h["ADCBITS"] = (int(depth), "sensor ADC bit depth")
    if bayer:
        h["BAYERPAT"] = (bayer, "CFA pattern as stored (measured from pixels)")
        h["XBAYROFF"] = 0
        h["YBAYROFF"] = 0
        sensor = _sensor_bayer_pattern(manifest)
        if sensor and sensor != bayer:
            h["SENSPAT"] = (sensor, "CFA pattern of the sensor per scope manifest")
    obs_start = _num(manifest.get("obs_start"))
    if obs_start is not None and obs_start > 0:
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

    # Bayer pattern: measured from the first light's pixels (the export's 2x2
    # phases do not match the sensor's -- see _measure_bayer_pattern).
    sample = tifffile.imread(light_tiffs[0]) if light_tiffs else None
    bayer = _bayer_pattern(manifest, sample)

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
    replace_retry(partial, obs_dir)  # same Defender race as the zip rename

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
