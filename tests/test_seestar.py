"""The Seestar pull: a folder sync, dated by filename, read-only on the scope.

Ported from starstack's test_seestar_pull_syncs_subs_by_night (v0.3.0) — same
synthetic MyWorks, same assertions, no hardware.
"""

from __future__ import annotations

from pathlib import Path

from scopepull.config import Config
from scopepull.log import Log, quiet
from scopepull.scopes import EXIT_NOTHING_NEW, EXIT_OK, EXIT_UNREACHABLE
from scopepull.scopes.seestar import (
    SeestarScope,
    find_seestar,
    myworks,
    night_of,
    pull_seestar,
)


def _myworks(tmp_path: Path) -> Path:
    """A Seestar's eMMC as it really looks: <target>_sub/ of subs with JPEG and
    thumbnail twins, and <target>/ holding the scope's own stack."""
    mw = tmp_path / "EMMC Images" / "MyWorks"

    def sub(target: str, stamps: list[str]) -> None:
        d = mw / f"{target}_sub"
        d.mkdir(parents=True)
        for st in stamps:
            stem = f"Light_{target}_10.0s_LP_{st}"
            (d / f"{stem}.fit").write_bytes(b"SIMPLE" + b"\0" * 100)
            (d / f"{stem}.jpg").write_bytes(b"jpg")
            (d / f"{stem}_thn.jpg").write_bytes(b"thn")
        r = mw / target
        r.mkdir()
        (r / f"Stacked_{len(stamps)}_{target}_10.0s_LP_{stamps[-1]}.fit").write_bytes(b"STACK")

    sub("NGC 7000", ["20260922-203537", "20260922-203551"])
    sub("M 31", ["20260919-235900", "20260920-001200"])  # straddles midnight: one night
    sub("M 31_mosaic", ["20260919-230000"])
    sub("Lunar_photo", ["20260922-201600"])
    return mw


def test_night_splits_at_noon_not_midnight():
    assert night_of("Light_M 31_10.0s_LP_20260922-203537.fit") == "2026-09-22"
    assert night_of("Light_M 31_10.0s_LP_20260920-001200.fit") == "2026-09-19"
    assert night_of("no stamp here.fit") is None
    assert night_of("Light_x_20261345-250000.fit") is None  # not a date


def test_myworks_found_from_drive_root_share_or_itself(tmp_path):
    mw = _myworks(tmp_path)
    assert myworks(tmp_path) == mw  # drive root -> EMMC Images -> MyWorks
    assert myworks(tmp_path / "EMMC Images") == mw
    assert myworks(mw) == mw
    assert myworks(tmp_path / "nothing") is None


def test_pull_syncs_subs_by_night(tmp_path):
    mw = _myworks(tmp_path)
    root = tmp_path / "archive"
    res = pull_seestar(mw, root, quiet())

    assert (res.copied, res.skipped) == (5, 0)
    assert res.nights == {"2026-09-22", "2026-09-19"} and res.code == EXIT_OK
    assert sorted(p.name for p in (root / "2026-09-22" / "NGC 7000").iterdir()) == [
        "Light_NGC 7000_10.0s_LP_20260922-203537.fit",
        "Light_NGC 7000_10.0s_LP_20260922-203551.fit",
    ]
    assert len(list((root / "2026-09-19" / "M 31").iterdir())) == 2  # one night, not two
    assert (root / "2026-09-19" / "M 31_mosaic").exists()  # mosaics come too
    assert not any("Lunar" in p.name for p in root.rglob("*"))
    assert not list(root.rglob("*.jpg")) and not list(root.rglob("Stacked_*"))
    assert not list(root.rglob("*.part"))

    # Second pull: the archive already has everything, and says so with exit 2.
    again = pull_seestar(mw, root, quiet())
    assert (again.copied, again.skipped) == (0, 5) and again.code == EXIT_NOTHING_NEW


def test_pull_target_filter(tmp_path):
    mw = _myworks(tmp_path)
    root = tmp_path / "archive"
    res = pull_seestar(mw, root, quiet(), target="ngc")
    assert res.copied == 2 and res.nights == {"2026-09-22"}
    assert not (root / "2026-09-19").exists()


def test_pull_leaves_the_scope_alone(tmp_path):
    mw = _myworks(tmp_path)
    before = sorted(str(p.relative_to(mw)) for p in mw.rglob("*"))
    pull_seestar(mw, tmp_path / "archive", quiet())
    assert sorted(str(p.relative_to(mw)) for p in mw.rglob("*")) == before


def test_find_takes_a_path_and_says_so(tmp_path):
    mw = _myworks(tmp_path)
    found = find_seestar(str(mw), quiet())
    assert found and found.handle == mw and str(mw) in found.how

    missing = find_seestar(str(tmp_path / "elsewhere"), quiet())
    assert not missing and missing.code == EXIT_UNREACHABLE
    assert "no MyWorks folder" in missing.how


def test_scope_object_narrates_and_keeps_its_own_root(tmp_path):
    mw = _myworks(tmp_path)
    cfg = Config(archive_roots={"seestar": tmp_path / "seestar"})
    sc = SeestarScope(cfg)
    assert sc.archive_root() == tmp_path / "seestar"

    lines: list[str] = []
    res = sc.pull(mw, sc.archive_root(), Log(lines.append))
    assert res.code == EXIT_OK
    assert any("5 new subs" in line or "pulled 5 new subs" in line for line in lines)
    assert any("skipped (not a deep-sky stack)" in line for line in lines)
