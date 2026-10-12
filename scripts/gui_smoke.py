#!/usr/bin/env python3
"""Open the owl, press the button, check what came back.

A script, not a pytest: it needs a display (on Linux, `xvfb-run -a python
scripts/gui_smoke.py`), and what it is really testing is the window — that the
faces load, that the footer buttons are inside the window at the default size,
that a press runs a real pull through the real CLI, and that the archive table
fills from `scopepull index` afterwards. The footer check earns its keep: in
starstack the three footer buttons were off-screen for two weeks at anything
but full-screen, and no unit test would ever have noticed.

The scope is synthetic: a MyWorks folder of fake Seestar subs in a temp dir.
Nothing touches a real telescope, a real archive, or your saved settings.

    python scripts/gui_smoke.py [--keep-open SECONDS] [--shot out.png]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scopepull import gui  # noqa: E402

STAMPS = {
    "NGC 7000": ["20260922-203537", "20260922-203551", "20260922-203604"],
    "M 31": ["20260919-235900", "20260920-001200"],
    "Lunar_photo": ["20260922-201600"],
}


def make_myworks(base: Path) -> Path:
    mw = base / "EMMC Images" / "MyWorks"
    for target, stamps in STAMPS.items():
        d = mw / f"{target}_sub"
        d.mkdir(parents=True)
        for st in stamps:
            stem = f"Light_{target}_10.0s_LP_{st}"
            (d / f"{stem}.fit").write_bytes(b"SIMPLE  =" + b"\0" * 4000)
            (d / f"{stem}.jpg").write_bytes(b"jpg")
    return mw


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def wait_for(app, predicate, seconds: float, what: str) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        if predicate():
            return
        time.sleep(0.05)
    raise SystemExit(f"FAIL: timed out after {seconds:.0f}s waiting for {what}")


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-open", type=float, default=0.0, help="leave it up for a look")
    ap.add_argument("--shot", help="save a screenshot here (needs pillow)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="owl-smoke-"))
    mw = make_myworks(tmp / "scope")
    archive = tmp / "archive"
    archive.mkdir()
    # Never read or write the real settings file.
    gui.SETTINGS_FILE = str(tmp / "settings.json")
    gui.OLD_SETTINGS_FILE = str(tmp / "nothing-here.json")

    print(f"scope:   {mw}")
    print(f"archive: {archive}")
    app = gui.App()
    app.update()

    print("the window")
    check(app.title() == "scopepull", "titled scopepull")
    check(len(app.faces) >= 6, f"{len(app.faces)} owl faces loaded")
    check(app.button.label in ("PULL", "STACK"), f"button says {app.button.label}")

    # the footer has to be inside the window at the default size
    app.geometry("960x800")
    pump(app, 0.6)
    bottom = app.winfo_rooty() + app.winfo_height()
    for name in ("sw_btn", "open_btn", "stop_btn"):
        w = getattr(app, name)
        inside = w.winfo_ismapped() and (w.winfo_rooty() + w.winfo_height()) <= bottom
        check(inside, f"{name} is on screen")

    print("the scope rows")
    wait_for(
        app,
        lambda: "reading the archive" not in app.scope_lbl.cget("text"),
        90,
        "scopepull status --json",
    )
    check(bool(app.roots.get("odyssey")), f"odyssey root: {app.roots.get('odyssey')}")
    check(bool(app.roots.get("seestar")), f"seestar root: {app.roots.get('seestar')}")

    # point it at the synthetic scope and the temp archive, and press
    app.seestar_at.set(str(mw))
    app.seestar_var.set(True)
    app._seestar_changed()
    app.folder.set(str(archive))
    pump(app, 0.4)
    # Wait for the probe of OUR folder, not whichever Seestar may really be on
    # the network -- the first probe fires before we fill the box in.
    wait_for(
        app,
        lambda: "owl-smoke" in str(app.seestar_found),
        120,
        "the Seestar probe to find the synthetic MyWorks",
    )
    check("owl-smoke" in str(app.seestar_found), f"found it: {app.seestar_found}")
    check(app.button.label == "PULL", "the button now says PULL")

    print("the press")
    app.press()
    wait_for(app, lambda: app.running(), 30, "the pull to start")
    wait_for(app, lambda: not app.running(), 300, "the pull to finish")
    pump(app, 2.0)  # let the queue drain and the table refill
    log = app.log.get("1.0", "end-1c")
    check("NGC 7000" in log, "NGC 7000 pulled")
    check("skipped (not a deep-sky stack)" in log, "the lunar folder was left behind")
    pulled = sorted(p.name for p in archive.rglob("*.fit"))
    check(len(pulled) == 5, f"{len(pulled)} subs in the archive")
    check(not list(archive.rglob("*.jpg")), "no JPEGs came across")

    print("the archive table")
    # two deep-sky targets in the synthetic scope; the lunar folder is not one
    wait_for(app, lambda: len(app.archive_rows) >= 2, 180, "the table to fill from `index`")
    targets = {r["target"] for r in app.archive_rows}
    check({"NGC 7000", "M 31"} <= targets, f"targets: {sorted(targets)}")
    rows = app.tree.get_children()
    check(bool(rows), f"{len(rows)} target rows in the tree")
    app.tree.selection_set(rows[0])
    app.update()
    check(str(app.open_btn.cget("state")) == "normal", "selecting a row enables the hand-off")

    if args.shot:
        try:
            import PIL.ImageGrab

            x, y = app.winfo_rootx(), app.winfo_rooty()
            PIL.ImageGrab.grab((x, y, x + app.winfo_width(), y + app.winfo_height())).save(
                args.shot
            )
            print(f"  shot: {args.shot}")
        except Exception as e:
            print(f"  (no screenshot: {e})")

    if args.keep_open:
        print(f"leaving it up for {args.keep_open:.0f}s")
        pump(app, args.keep_open)

    app.quit_app()
    print("\nthe owl is fine.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
