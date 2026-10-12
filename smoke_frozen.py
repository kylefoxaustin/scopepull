#!/usr/bin/env python3
"""Run the built scopepull.exe against a synthetic scope before releasing it.

    python smoke_frozen.py dist/scopepull.exe

Nothing is released that hasn't done a real pull. A frozen build can break in
ways no unit test sees: a missing data file, a module PyInstaller didn't
notice, an import that works from source and not from the archive. starstack
shipped nine releases with `import astropy` broken inside its exe -- every FITS
frame "unreadable", TIFFs fine, nobody the wiser until a user said so. So this
checks the one thing that proves astropy is alive in there: the exposure of a
Seestar sub, which can only be read out of a FITS header.

Standard library only, so it runs on a bare runner, and it builds its own FITS
by hand for the same reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

EXPTIME = 10.0
NIGHTS = {
    "NGC 7000": ["20260922-203537", "20260922-203551", "20260922-203604"],
    "M 31": ["20261004-210001", "20261004-210015"],
    "Lunar_photo": ["20260922-201600"],  # not a deep-sky stack: must stay behind
}


def fits_bytes(width: int = 16, height: int = 12, exptime: float = EXPTIME) -> bytes:
    """A minimal, real FITS file: 16-bit, two axes, with an EXPTIME to find."""

    def card(key: str, value: str) -> str:
        return f"{key:<8}= {value:>20}".ljust(80)

    cards = [
        card("SIMPLE", "T"),
        card("BITPIX", "16"),
        card("NAXIS", "2"),
        card("NAXIS1", str(width)),
        card("NAXIS2", str(height)),
        card("BZERO", "32768"),
        card("BSCALE", "1"),
        card("EXPTIME", f"{exptime:.1f}"),
        f"{'BAYERPAT':<8}= 'GRBG    '".ljust(80),
        "END".ljust(80),
    ]
    header = "".join(cards)
    header += " " * (-len(header) % 2880)
    data = b"\x00\x00" * (width * height)
    data += b"\x00" * (-len(data) % 2880)
    return header.encode("ascii") + data


def make_myworks(base: Path) -> Path:
    mw = base / "EMMC Images" / "MyWorks"
    for target, stamps in NIGHTS.items():
        d = mw / f"{target}_sub"
        d.mkdir(parents=True)
        for st in stamps:
            stem = f"Light_{target}_10.0s_LP_{st}"
            (d / f"{stem}.fit").write_bytes(fits_bytes())
            (d / f"{stem}.jpg").write_bytes(b"jpg")
            (d / f"{stem}_thn.jpg").write_bytes(b"thn")
        r = mw / target
        r.mkdir()
        (r / f"Stacked_{len(stamps)}_{target}.fit").write_bytes(fits_bytes())
    return mw


class Smoke:
    def __init__(self, exe: Path) -> None:
        self.exe = exe
        self.failed = 0

    def run(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.exe), "--cli", *args]
        print(f"\n$ {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines()[:25]:
            print(f"    {line}")
        self.check(r.returncode == expect, f"exit {r.returncode} (wanted {expect})")
        return r

    def check(self, cond: bool, what: str) -> bool:
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            self.failed += 1
        return cond


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    exe = Path(argv[0]).resolve()
    if not exe.is_file():
        print(f"no such exe: {exe}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="scopepull-frozen-"))
    mw = make_myworks(tmp / "scope")
    archive = tmp / "archive"
    s = Smoke(exe)
    print(f"exe:     {exe}  ({exe.stat().st_size / 2**20:.1f} MiB)")
    print(f"scope:   {mw}")
    print(f"archive: {archive}")

    r = s.run("--version")
    s.check("scopepull" in r.stdout, f"it knows its name: {r.stdout.strip()!r}")

    r = s.run("status", "--json")
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
        s.check("odyssey" in (data.get("roots") or {}), "status --json has the archive roots")
    except (ValueError, IndexError):
        s.check(False, "status --json is JSON")

    r = s.run("find", "--scope", "seestar", "--at", str(mw))
    s.check("found" in r.stdout.lower(), "it found the synthetic Seestar")

    s.run("pull", "--scope", "seestar", "--at", str(mw), "--dest", str(archive))
    subs = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*.fit"))
    s.check(len(subs) == 5, f"{len(subs)} subs pulled (wanted 5)")
    s.check(
        subs[:1] == ["2026-09-22/NGC 7000/Light_NGC 7000_10.0s_LP_20260922-203537.fit"]
        or any("NGC 7000" in p for p in subs),
        "filed by night and target",
    )
    s.check(not list(archive.rglob("*.jpg")), "no JPEG twins came across")
    s.check(not list(archive.rglob("Stacked_*")), "the scope's own stack stayed behind")
    s.check(not list(archive.rglob("*Lunar*")), "the lunar folder stayed behind")
    s.check(not list(archive.rglob("*.part")), "no half-written files left")

    # Nothing new the second time: exit 2, the code starstack and the window
    # both read as "the archive is worth stacking, there's just nothing new".
    s.run("pull", "--scope", "seestar", "--at", str(mw), "--dest", str(archive), expect=2)

    r = s.run("index", "--scope", "seestar", "--root", str(archive))
    rows = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
    s.check(len(rows) == 2, f"{len(rows)} sessions indexed (wanted 2)")
    # THE astropy check: exposure_s can only come from the FITS header, read by
    # astropy, inside this exe. 0.0 here means `import astropy` is broken in
    # the frozen build and every FITS is silently unreadable.
    exposures = sorted({row["exposure_s"] for row in rows})
    s.check(
        exposures == [EXPTIME],
        f"exposure read from the FITS headers: {exposures} (wanted [{EXPTIME}]) "
        "-- 0.0 means astropy is broken inside the exe",
    )
    s.check(
        all(row["integration_s"] == row["frames"] * EXPTIME for row in rows),
        "integration = frames x exposure",
    )

    r = s.run("combine", "NGC 7000", "--scope", "seestar", "--root", str(archive))
    tag = "combined: "
    dest = [ln[len(tag) :].strip() for ln in r.stdout.splitlines() if ln.startswith(tag)]
    s.check(bool(dest), "combine printed where it put them")
    if dest:
        linked = sorted(Path(dest[0]).glob("*.fit"))
        s.check(len(linked) == 3, f"{len(linked)} frames combined (wanted 3)")

    print("\n" + ("the owl is fine." if not s.failed else f"{s.failed} check(s) FAILED"))
    return 1 if s.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
