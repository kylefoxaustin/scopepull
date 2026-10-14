"""The ZWO Seestar S50 / S50 Pro: a folder, not a portal.

A Seestar keeps every target twice: `MyWorks/<target>/` holds its own stacked
result, and `MyWorks/<target>_sub/` holds the sub-frames, one `.fit` plus a
`.jpg` twin and a `_thn.jpg` thumbnail each. Plugged in over USB the eMMC mounts
as a drive letter; on Wi-Fi in station mode it's a passwordless guest Samba
share, `\\\\seestar\\EMMC Images`. Either way the pull is a copy: every `.fit`
the archive hasn't got, into `<root>/<night>/<target>/`. Nothing to install on
the scope, no API to version, and the scope is only ever read.

Left behind deliberately: the `.jpg` twins and `_thn.jpg` thumbnails (they're
previews, not data), the scope's own `Stacked_*.fit`, and the `Lunar_*`,
`Solar_*` and `*_video` folders (not deep-sky stacks). `*_mosaic` targets DO
come across — framing mode is real data, and StackingWizard tiles them.

Lifted from starstack v0.3.0, verified there against Kyle's own S50 Pro over
both USB (`D:\\MyWorks`) and Wi-Fi (`\\\\192.168.1.50\\EMMC Images`): 17 sessions,
6h 17m. The one change on arrival is that the copy commits through
fsutil.replace_retry, because this repo learned the hard way that Windows
Defender holds a file open just long enough to fail the rename (0.1.1).

Seestar sub headers, for reference: 3840x2160 16-bit (S50: 1080x1920),
BAYERPAT 'GRBG' (honest, unlike the Odyssey's), XPIXSZ 2.9, FOCALLEN 260,
RA/DEC per frame, FILTER LP or IRCUT.
"""

from __future__ import annotations

import datetime
import re
import shutil
import socket
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import Config
from ..fsutil import replace_retry, unlink_retry
from ..log import Log
from . import (
    EXIT_NOTHING_NEW,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_UNREACHABLE,
    LABELS,
    Found,
    PullResult,
)

SEESTAR_SHARE = "EMMC Images"
SEESTAR_HOSTS = ("seestar", "seestar.local")
SEESTAR_SKIP = ("lunar_", "solar_", "_video")
SUB_SUFFIX = "_sub"
# Consecutive copy failures that mean "the scope went away" rather than "that
# file is bad". Low enough to stop a 600-sub night quickly, high enough to ride
# out a couple of genuinely unreadable frames.
GIVE_UP_AFTER = 6
FITS_SUFFIXES = (".fit", ".fits")
_STAMP = re.compile(r"_(\d{4})(\d{2})(\d{2})-(\d{2})\d{4}")

NOT_FOUND_HELP = (
    "  USB: plug the scope in and wait for its drive to appear.",
    "  Wi-Fi: station mode on in the Seestar app, laptop on the same network.",
    "  If Windows still can't see it, see 'Seestar over Wi-Fi' in the README",
    "  (two one-line SMB settings, once per machine).",
)


def _is_pathish(spec: str) -> bool:
    """Did the user hand us a place or a name? `D:`, `D:\\`, `/media/seestar`
    and `\\\\192.168.1.50\\EMMC Images` are places; `seestar` and `192.168.1.50`
    are names to resolve."""
    return (
        spec.startswith("\\\\")
        or "/" in spec
        or "\\" in spec
        or (len(spec) == 2 and spec[1] == ":")
    )


def myworks(path: str | Path) -> Path | None:
    """Given a drive, a share, the scope's folder or MyWorks itself, return the
    MyWorks folder if it's there and readable, else None. A folder is MyWorks
    when something inside it ends in `_sub`."""
    if not path:
        return None
    base = Path(str(path).strip().strip('"')).expanduser()
    for cand in (base, base / "MyWorks", base / SEESTAR_SHARE / "MyWorks"):
        try:
            if cand.is_dir() and any(p.name.lower().endswith(SUB_SUFFIX) for p in cand.iterdir()):
                return cand
        except OSError:
            continue
    return None


def _smb_guest(host: str, log: Log | None = None) -> bool:
    """Windows won't open a passwordless share without being told to. Make the
    guest session for the scope's IPC$ ourselves — the `net use ... /user:guest`
    everyone ends up typing — silently, and then the UNC path just works.

    Two one-time Windows 11 24H2+ settings still have to be on for this to take
    (RequireSecuritySignature off, EnableInsecureGuestLogons on); the README
    says so in plain words. On anything but Windows the OS mounts shares its own
    way and there's nothing to do here."""
    if sys.platform != "win32":
        return True
    try:
        r = subprocess.run(
            ["net", "use", rf"\\{host}\IPC$", "", "/user:guest"],
            input="\n",
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0 and log:
            detail = (r.stderr or r.stdout).strip()[:120]
            log(rf"  (guest login to \\{host} refused: {detail})")
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _unc(ip: str) -> str:
    if sys.platform == "win32":
        return rf"\\{ip}\{SEESTAR_SHARE}"
    return f"//{ip}/{SEESTAR_SHARE}"


def find_seestar(spec: str | None = None, log: Log | None = None) -> Found:
    """Where is the scope? `spec` is whatever the user gave `--at`: nothing (go
    looking), a drive or folder (USB), a UNC path, or a hostname/IP (Wi-Fi)."""
    tried: list[str] = []
    if spec:
        spec = spec.strip().strip('"')
        if _is_pathish(spec):
            if spec.startswith("\\\\"):
                _smb_guest(spec.strip("\\").split("\\")[0], log)
            mw = myworks(spec)
            if mw:
                return Found(mw, f"at {spec}")
            return Found(None, f"no MyWorks folder at {spec}", EXIT_UNREACHABLE)
        hosts = [spec]
    else:
        # USB first: a drive whose root holds MyWorks. Only Windows mounts the
        # eMMC as a letter; on Linux/macOS it lands under /media or /Volumes,
        # which the user can pass with --at.
        if sys.platform == "win32":
            for letter in string.ascii_uppercase[3:]:  # C: is never the scope
                mw = myworks(f"{letter}:\\")
                if mw:
                    return Found(mw, f"on USB, drive {letter}:")
        hosts = list(SEESTAR_HOSTS)

    for h in hosts:
        ip = _resolve(h)
        if not ip:
            tried.append(f"{h}: no such host")
            continue
        _smb_guest(ip, log)
        mw = myworks(_unc(ip))
        if mw:
            return Found(mw, f"on Wi-Fi at {h} ({ip})")
        tried.append(f"{h} ({ip}): answers, but no share the owl can read")
    return Found(None, "; ".join(tried) if tried else "nothing given", EXIT_UNREACHABLE)


def night_of(name: str) -> str | None:
    """The night a sub belongs to, from its filename:
    `Light_M 31_10.0s_LP_20260922-203537.fit` -> 2026-09-22. Anything before
    noon belongs to the evening before, so one session never splits in two at
    midnight."""
    m = _STAMP.search(name)
    if not m:
        return None
    y, mo, d, hh = (int(x) for x in m.groups())
    try:
        day = datetime.date(y, mo, d)
    except ValueError:
        return None
    if hh < 12:
        day -= datetime.timedelta(days=1)
    return day.isoformat()


def _sub_dirs(mw: Path) -> list[Path] | None:
    """The `<target>_sub` folders, or None if MyWorks itself can't be read --
    the scope is allowed to vanish between `find` and `pull` (USB unplugged,
    Wi-Fi dropped), and that is an unreachable scope, not a crash."""
    try:
        return sorted(p for p in mw.iterdir() if p.is_dir() and p.name.lower().endswith(SUB_SUFFIX))
    except OSError:
        return None


def pull_seestar(mw: Path, root: Path, log: Log, target: str | None = None) -> PullResult:
    """Copy every sub-frame the archive hasn't got. Read-only on the scope."""
    res = PullResult()
    subdirs = _sub_dirs(mw)
    if subdirs is None:
        log(f"the scope stopped answering at {mw} — unplugged, or the Wi-Fi dropped.")
        res.code = EXIT_UNREACHABLE
        return res
    for sub in subdirs:
        tname = sub.name[: -len(SUB_SUFFIX)]
        low = tname.lower()
        if any(k in low for k in SEESTAR_SKIP):
            log(f"  {tname}: skipped (not a deep-sky stack)")
            continue
        if target and target.lower() not in low:
            continue
        try:
            subs = sorted(p for p in sub.iterdir() if p.suffix.lower() in FITS_SUFFIXES)
        except OSError as e:
            log(f"  {tname}: can't list: {e}")
            res.failed.append(tname)
            continue

        new: list[tuple[Path, Path, str]] = []
        for src in subs:
            night = night_of(src.name) or "undated"
            dest = root / night / tname / src.name
            try:
                if dest.exists() and dest.stat().st_size == src.stat().st_size:
                    res.skipped += 1
                    continue
            except OSError:
                pass
            new.append((src, dest, night))
        if not new:
            log(f"  {tname}: {len(subs)} subs, nothing new")
            continue

        log(f"  {tname}: {len(new)} new sub{'s' if len(new) != 1 else ''} of {len(subs)}")
        touched: set[Path] = set()
        in_a_row = 0
        for i, (src, dest, night) in enumerate(new, 1):
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                # The archive side, not the scope: a full or read-only disk.
                log(f"    can't make {dest.parent}: {e}")
                res.failed.append(src.name)
                res.code = EXIT_PARTIAL
                return res
            part = dest.with_name(dest.name + ".part")
            try:
                shutil.copyfile(src, part)
                # Not os.replace: Windows Defender scans the file we just wrote
                # and holds it open long enough to fail the rename (0.1.1).
                replace_retry(part, dest)
            except OSError as e:
                log(f"    {src.name}: {e}")
                unlink_retry(part)
                res.failed.append(src.name)
                in_a_row += 1
                # A dropped share fails every remaining sub, each one waiting out
                # an SMB reconnect first. Six in a row is the scope leaving, not
                # six bad files: stop and say so.
                if in_a_row >= GIVE_UP_AFTER and not src.exists():
                    log(
                        f"  {tname}: {in_a_row} failures in a row and the scope is no longer "
                        f"readable — stopping. What landed is in the archive; run it again."
                    )
                    res.code = EXIT_UNREACHABLE
                    return res
                continue
            in_a_row = 0
            res.copied += 1
            res.nights.add(night)
            touched.add(dest.parent)
            log.raw(f"    copying {i}/{len(new)}\r")
        log.raw("\n")
        for d in sorted(touched):
            log(f"    into {d}")

    if res.failed:
        res.code = EXIT_PARTIAL
    elif res.copied:
        res.code = EXIT_OK
    else:
        res.code = EXIT_NOTHING_NEW
    return res


class SeestarScope:
    """`--scope seestar`."""

    name = "seestar"
    label = LABELS["seestar"]

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def archive_root(self) -> Path:
        return self.cfg.root_for(self.name)

    def find(self, spec: str | None, log: Log) -> Found:
        found = find_seestar(spec, log)
        if not found:
            # The way out of "can't find it" is knowledge no exit code carries,
            # so it rides along in `how` — printed once, by whoever asked, in
            # the right order.
            found.how = found.how + "\n" + "\n".join(NOT_FOUND_HELP)
        return found

    def pull(self, handle: Any, root: Path, log: Log, target: str | None = None) -> PullResult:
        res = pull_seestar(Path(handle), root, log, target)
        if res.copied:
            log(
                f"pulled {res.copied} new sub{'s' if res.copied != 1 else ''} "
                f"({res.skipped} already had)."
            )
        else:
            log(f"nothing new on the scope ({res.skipped} subs already in the archive).")
        return res
