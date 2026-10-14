"""The archive as data: index both layouts, combine nights into one folder.

Ported from starstack's test_archive_index_and_combine and the archive-layout
half of test_scopepull_archive_layout (v0.3.0).
"""

from __future__ import annotations

import json

import numpy as np
from astropy.io import fits

from scopepull.archive import (
    archive_index,
    combine_target,
    prefer_fits,
    session_name,
    summarize,
)


def _seestar_night(root, night, target, stamps):
    d = root / night / target
    d.mkdir(parents=True)
    for st in stamps:
        hdu = fits.PrimaryHDU(np.zeros((12, 16), dtype=np.uint16))
        hdu.header["BAYERPAT"] = "GRBG"
        hdu.header["EXPTIME"] = 10.0
        hdu.writeto(d / f"Light_{target}_10.0s_LP_{st}.fit")
        (d / f"Light_{target}_10.0s_LP_{st}.jpg").write_bytes(b"jpg")  # must not count


def _odyssey_observation(root, night, folder, target, frames, expo_us=4_000_000):
    d = root / night / folder
    (d / "frames").mkdir(parents=True)
    (d / "calibration").mkdir()
    (d / "reference").mkdir()
    for i in range(frames):
        # scopepull writes both: the scope's TIFF and a FITS of the same pixels.
        (d / "frames" / f"{i:03d}_StackInput.tiff").write_bytes(b"II*\0")
        (d / "frames" / f"{i:03d}_StackInput.fits").write_bytes(b"SIMPLE")
    (d / "calibration" / "DarkFrameMean.fits").write_bytes(b"SIMPLE")
    (d / "reference" / "StackSum.tiff").write_bytes(b"II*\0")
    (d / "reference" / "preview.jpg").write_bytes(b"jpg")
    (d / "observation.json").write_text(
        json.dumps({"scope_manifest": {"nameTarget": target, "expo": expo_us}}),
        encoding="utf-8",
    )
    return d


def test_index_a_seestar_archive(tmp_path):
    root = tmp_path / "seestar"
    _seestar_night(
        root, "2026-09-22", "NGC 7000", ["20260922-203537", "20260922-203551", "20260922-203604"]
    )
    _seestar_night(root, "2026-10-04", "NGC 7000", ["20261004-210001", "20261004-210015"])
    _seestar_night(root, "2026-10-04", "M 27", ["20261004-220001"])
    (root / "stacks").mkdir()
    (root / "stacks" / "M 27.tif").write_bytes(b"x")

    rows = archive_index(root, scope="seestar")
    assert [(r.night, r.target, r.frames, r.integration_s, r.stacked) for r in rows] == [
        ("2026-10-04", "M 27", 1, 10.0, True),
        ("2026-10-04", "NGC 7000", 2, 20.0, False),
        ("2026-09-22", "NGC 7000", 3, 30.0, False),
    ]
    assert all(r.scope == "seestar" and r.pulled_at > 0 for r in rows)
    assert summarize(rows) == "3 sessions, 2 nights, 6 frames, 0h 01m"


def test_index_an_odyssey_archive(tmp_path):
    root = tmp_path / "odyssey"
    _odyssey_observation(
        root, "2026-01-31", "m81-bode-s-galaxy__38102043", "M81 - Bode's Galaxy", 8
    )
    _odyssey_observation(root, "2026-02-01", "m27__38102099", "M27 - Dumbbell Nebula", 6)
    # A pull still in progress is not ours yet.
    (root / "2026-02-01" / "m13__38102100.partial" / "frames").mkdir(parents=True)
    (root / "2026-02-01" / "m13__38102100.partial" / "frames" / "x_StackInput.tiff").write_bytes(
        b"j"
    )

    rows = archive_index(root, scope="odyssey")
    assert [(r.night, r.target, r.frames) for r in rows] == [
        ("2026-02-01", "M27 - Dumbbell Nebula", 6),  # the TIFF twins counted once
        ("2026-01-31", "M81 - Bode's Galaxy", 8),
    ]
    # expo is microseconds in the scope's own manifest
    assert rows[0].exposure_s == 4.0 and rows[0].integration_s == 24.0
    assert (
        session_name(root / "2026-01-31" / "m81-bode-s-galaxy__38102043") == "M81 - Bode's Galaxy"
    )


def test_prefer_fits_sets_aside_the_tiff_twins(tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    for i in range(3):
        (d / f"{i}_StackInput.tiff").touch()
        (d / f"{i}_StackInput.fits").touch()
    (d / "lonely.tiff").touch()
    kept, dropped = prefer_fits(sorted(d.iterdir()))
    assert dropped == 3
    assert sorted(p.name for p in kept) == [
        "0_StackInput.fits",
        "1_StackInput.fits",
        "2_StackInput.fits",
        "lonely.tiff",
    ]


def test_combine_gathers_nights_as_hard_links(tmp_path):
    root = tmp_path / "seestar"
    _seestar_night(
        root, "2026-09-22", "NGC 7000", ["20260922-203537", "20260922-203551", "20260922-203604"]
    )
    _seestar_night(root, "2026-10-04", "NGC 7000", ["20261004-210001", "20261004-210015"])
    lines: list[str] = []
    from scopepull.log import Log

    log = Log(lines.append)

    dest = combine_target(root, "NGC 7000", log=log)
    assert dest == root / "combined" / "NGC 7000"
    names = sorted(p.name for p in dest.iterdir())
    assert len(names) == 5 and all(n.endswith(".fit") for n in names)
    assert (dest / names[0]).stat().st_nlink == 2  # a link, not a copy
    assert "2 nights" in lines[0] and "5 frames" in lines[0]

    # Idempotent, and the nights are untouched.
    assert combine_target(root, "NGC 7000", log=log) == dest
    assert len(list(dest.iterdir())) == 5
    assert len(list((root / "2026-09-22" / "NGC 7000").glob("*.fit"))) == 3

    assert combine_target(root, "NGC 7000", nights=["2026-10-04"], log=log) == dest
    assert combine_target(root, "nothing out there", log=log) is None

    # combined/ is output, not a night: the index doesn't list it again.
    assert len(archive_index(root)) == 2


def test_a_target_named_final_or_master_is_not_swallowed(tmp_path):
    """The Seestar puts the target name in every sub's filename, so matching
    "final"/"stacked"/"master" anywhere would make these targets vanish from
    the index with no explanation."""
    root = tmp_path / "seestar"
    _seestar_night(root, "2026-10-04", "M31 final", ["20261004-210001", "20261004-210015"])
    _seestar_night(root, "2026-10-04", "Master Ring", ["20261004-220001"])
    # ...while the scope's own stack and the thumbnails still don't count
    d = root / "2026-10-04" / "M31 final"
    (d / "Stacked_2_M31 final_10.0s_LP_20261004-210015.fit").write_bytes(b"SIMPLE")
    (d / "Light_M31 final_10.0s_LP_20261004-210001_thn.jpg").write_bytes(b"thn")

    rows = {r.target: r.frames for r in archive_index(root)}
    assert rows == {"M31 final": 2, "Master Ring": 1}
