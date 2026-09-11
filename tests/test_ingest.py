"""ingest_zip: layout, TIFF->FITS, transactional commit, manifest row."""

from __future__ import annotations

import numpy as np
from astropy.io import fits

from scopepull.catalog import Observation
from scopepull.config import Config
from scopepull.ingest import ingest_zip
from scopepull.manifest import Manifest
from tests.mock_scope import OBSERVATIONS, make_calibration_zip


def _setup(tmp_path):
    zip_path = tmp_path / "obs.zip"
    zip_path.write_bytes(make_calibration_zip(OBSERVATIONS[0]))
    obs = Observation.from_raw(OBSERVATIONS[0])
    cfg = Config(archive_root=tmp_path / "archive", format="tiff")
    m = Manifest(tmp_path / "m.db")
    return zip_path, obs, cfg, m


def test_ingest_layout_and_fits(tmp_path):
    zip_path, obs, cfg, m = _setup(tmp_path)
    res = ingest_zip(zip_path, obs, cfg, m)
    d = res.dir
    assert d.exists() and not d.with_name(d.name + ".partial").exists()
    lights = list((d / "frames").glob("*.tiff"))
    fitses = list((d / "frames").glob("*.fits"))
    assert len(lights) == res.light_count == 2
    assert len(fitses) == 2
    assert (d / "calibration").glob("*.tiff")  # dark kept
    assert list((d / "calibration").glob("*.fits"))  # dark converted
    assert list((d / "reference").glob("*StackSum*"))
    assert (d / "reference" / "preview.jpg").exists()
    assert (d / "observation.json").exists()
    assert (d / "SHA256SUMS").exists()
    m.close()


def test_ingest_fits_headers(tmp_path):
    zip_path, obs, cfg, m = _setup(tmp_path)
    res = ingest_zip(zip_path, obs, cfg, m)
    f = sorted((res.dir / "frames").glob("*.fits"))[0]
    with fits.open(f) as hdul:
        h = hdul[0].header
        # measured from the pixels: real exports are RGGB as stored, even though
        # the manifest says the SENSOR is GBRG (see ingest._measure_bayer_pattern)
        assert h["BAYERPAT"] == "RGGB"
        assert h["SENSPAT"] == "GBRG"
        assert h["OBJECT"] == "M101 - Pinwheel Galaxy"
        assert h["INSTRUME"] == "IMX415"
        assert abs(h["EXPTIME"] - 3.999977) < 1e-3
        assert h["GAIN"] == 321
        assert hdul[0].data.dtype.name == "uint16"
    m.close()


def test_ingest_commits_manifest_row(tmp_path):
    zip_path, obs, cfg, m = _setup(tmp_path)
    assert not m.is_local(obs.obs_id)
    ingest_zip(zip_path, obs, cfg, m)
    assert m.is_local(obs.obs_id)
    n, total = m.totals()
    assert n == 1 and total > 0
    m.close()


def test_ingest_is_idempotent(tmp_path):
    zip_path, obs, cfg, m = _setup(tmp_path)
    ingest_zip(zip_path, obs, cfg, m)
    ingest_zip(zip_path, obs, cfg, m)  # re-ingest overwrites cleanly
    assert m.totals()[0] == 1
    m.close()


def test_ingest_skips_fits_when_disabled(tmp_path):
    zip_path, obs, cfg, m = _setup(tmp_path)
    res = ingest_zip(zip_path, obs, cfg, m, write_fits=False)
    assert res.fits_written == 0
    assert not list((res.dir / "frames").glob("*.fits"))
    m.close()


def test_bayer_pattern_is_measured_not_assumed():
    """The manifest's BAYER_GBRG is the sensor; the export's pixels decide."""
    import io

    import tifffile

    from scopepull.ingest import _bayer_pattern, _measure_bayer_pattern
    from tests.mock_scope import _bayer_frame

    manifest = {"type": "BAYER_GBRG"}
    anti = tifffile.imread(io.BytesIO(_bayer_frame(1, greens="anti")))
    main = tifffile.imread(io.BytesIO(_bayer_frame(1, greens="main")))
    assert _measure_bayer_pattern(anti, "GBRG") == "RGGB"  # what the real scope exports
    assert _measure_bayer_pattern(main, "GBRG") == "GBRG"  # sensor-order mosaic
    flat = np.full((120, 160), 20000, np.uint16)
    assert _measure_bayer_pattern(flat, "GBRG") is None  # mono / debayered (PlanetEV)
    assert _bayer_pattern(manifest, anti) == "RGGB"
    assert _bayer_pattern(manifest, flat) is None  # pixels overrule the manifest
    assert _bayer_pattern(manifest, None) == "RGGB"  # no sample: row-flipped sensor
    assert _bayer_pattern({"type": "DEBAYERED"}, None) is None
