#!/usr/bin/env python3
"""The owl: scopepull with a face.

Tick your scope. Press the button. The night lands in the archive below, and
you hand a night of it to a stacker. That's the entire manual.

Lifted from starstack's button.py (v0.3.0) when the owl moved in here -- see
docs/THE_OWL_MOVES_IN.md. Three weeks of layout pain are in this file: the
footer that was off-screen for two weeks, the job object that stops a closed
window leaving a download running, the `\\r` relay that lets a progress line
overwrite itself instead of scrolling the window off the bottom. It is kept
close to the original on purpose.

What changed on arrival: the window used to be starstack's face and shelled out
to scopepull for the Odyssey. Now it IS scopepull's face -- every pull, index
and combine goes through our own CLI (`python -m scopepull ...`, or the frozen
exe in --cli mode), and starstack became the optional quick-look stacker it
looks for on PATH. Without starstack the owl is purely a puller, and says so.

Standard library only (tkinter). It deliberately imports no numpy, no astropy
and nothing else from scopepull but __version__, so the window is up instantly
and stays up even if a dependency underneath is broken.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog

from . import __version__

FROZEN = getattr(sys, "frozen", False)  # running as scopepull.exe
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")
IMAGE_EXT = {".tif", ".tiff", ".fit", ".fits", ".fts", ".png", ".jpg", ".jpeg"}

NAVY, NAVY2, INK, CREAM, MUTE = "#0f172a", "#172033", "#0b1220", "#f4efe6", "#8a93ad"
RED, RED_DARK, RED_LIT = "#d7301c", "#8c1a0f", "#ff6b57"
MONO = ("Consolas", 10) if sys.platform.startswith("win") else ("DejaVu Sans Mono", 10)
SANS = ("Segoe UI", 11) if sys.platform.startswith("win") else ("DejaVu Sans", 11)

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

SCOPE_LABEL = {"odyssey": "Unistellar Odyssey Pro", "seestar": "ZWO Seestar S50 / S50 Pro"}
RELEASES = "https://github.com/kylefoxaustin/scopepull/releases"
STARSTACK_REPO = "https://github.com/kylefoxaustin/starstack"
STACKINGWIZARD_URL = "https://astrowizard.lukomatico.com/stackingwizard.html"


def self_cli() -> list[str]:
    """How to run our own CLI.

    Frozen, we are the CLI: scopepull.exe --cli <args>. Unfrozen, the
    interpreter running this window is the one that has scopepull importable,
    so -m scopepull is more reliable than hunting for a console script on a
    PATH that Explorer inherited at sign-in time.
    """
    if FROZEN:
        return [sys.executable, "--cli"]
    return [sys.executable, "-u", "-m", "scopepull"]


def has_images(folder):
    try:
        return any(os.path.splitext(f)[1].lower() in IMAGE_EXT for f in os.listdir(folder))
    except OSError:
        return False


def is_sorted(folder):
    """Sorted layout: a lights/ subfolder with frames in it (Seestar), or
    frames/ (a scopepull observation)."""
    for d in ("lights", "light", "frames"):
        p = os.path.join(folder, d)
        if os.path.isdir(p) and has_images(p):
            return True
    return False


def is_session(folder):
    return os.path.isdir(folder) and (has_images(folder) or is_sorted(folder))


def night_sessions(folder, deeper=True):
    """Session folders under a night, Seestar-aware: `M81/` (results) and
    `M81_sub/` (frames) count once, as the `_sub`. An archive root
    (<root>/<date>/<session>/) is a folder of nights; one level down."""
    out = []
    for d in sorted(os.listdir(folder)):
        p = os.path.join(folder, d)
        if not os.path.isdir(p) or d.lower().endswith(".partial"):
            continue
        if d.lower() == "stacks" and os.path.exists(os.path.join(p, "night.log")):
            continue  # our own earlier output
        if not is_session(p):
            if deeper and not has_images(p):
                out += night_sessions(p, deeper=False)
            continue
        if not d.lower().endswith("_sub") and os.path.isdir(p + "_sub") and has_images(p + "_sub"):
            continue
        out.append(p)
    return out


def is_night(folder):
    """A folder of session folders rather than a folder of frames."""
    if not os.path.isdir(folder) or is_session(folder):
        return False
    return bool(night_sessions(folder))


def target_name(folder):
    """Name the output after the target when the scope tells us."""
    name = None
    for fname, key in (("manifest.json", None), ("observation.json", "scope_manifest")):
        mpath = os.path.join(folder, fname)
        if os.path.exists(mpath):
            try:
                import json

                with open(mpath, encoding="utf-8") as fh:
                    mf = json.load(fh)
                name = ((mf.get(key) or {}) if key else mf).get("nameTarget")
                break
            except Exception:
                pass
    name = (name or os.path.basename(folder.rstrip("/\\"))).strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "-")
    return name.strip(" .") or "stack"


class Button(tk.Canvas):
    """The Big Red Button. Round, glossy, presses down."""

    def __init__(self, master, size=150, command=None, **kw):
        super().__init__(master, width=size, height=size, bg=NAVY, highlightthickness=0, **kw)
        self.size, self.command, self.enabled = size, command, True
        self.label = "PULL"
        self.draw(pressed=False)
        self.bind("<ButtonPress-1>", lambda e: self.enabled and self.draw(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)

    def _release(self, _):
        if not self.enabled:
            return
        self.draw(pressed=False)
        if self.command:
            self.command()

    def draw(self, pressed):
        self.delete("all")
        s = self.size
        c = s / 2
        r = s * 0.40
        lift = 0 if pressed else s * 0.05
        # bezel + well
        self.create_oval(
            c - r * 1.22,
            c - r * 1.22,
            c + r * 1.22,
            c + r * 1.22,
            fill="#5b6068",
            outline="#2a2e35",
            width=3,
        )
        self.create_oval(
            c - r * 1.08, c - r * 1.08, c + r * 1.08, c + r * 1.08, fill="#1c1f24", outline=""
        )
        # side of the cap, then the cap itself sitting higher
        col = RED if self.enabled else "#6b3a35"
        self.create_oval(
            c - r, c - r + lift * 0.4, c + r, c + r + lift * 0.4, fill=RED_DARK, outline=""
        )
        self.create_oval(c - r, c - r - lift, c + r, c + r - lift, fill=col, outline="")
        self.create_oval(
            c - r * 0.55,
            c - r * 0.85 - lift,
            c + r * 0.15,
            c - r * 0.45 - lift,
            fill=RED_LIT if self.enabled else "#8a5a55",
            outline="",
            stipple="gray50",
        )
        size_pt = int(
            s * (0.17 if len(self.label) <= 5 else 0.13 if len(self.label) <= 8 else 0.105)
        )
        self.create_text(
            c,
            c - lift + 2,
            text=self.label,
            fill="#fff4ee",
            font=("Impact", size_pt)
            if sys.platform.startswith("win")
            else ("DejaVu Sans", int(size_pt * 0.82), "bold"),
        )

    def set_enabled(self, on):
        self.enabled = on
        self.draw(pressed=False)

    def set_label(self, text):
        self.label = text
        self.draw(pressed=False)


# ---------------------------------------------------------------------------
# pausing and killing: freeze or end the pull and everything under it
# ---------------------------------------------------------------------------
def make_job():
    """Windows only: a job object marked KILL_ON_JOB_CLOSE. Every process we
    put in it -- the pull, a stacker, their workers -- is ended by Windows the
    moment the last handle to the job closes, and our handle closes when the
    owl dies, however it dies: X button, Task Manager, crash. That is the
    guarantee the X-button handler alone can't give (Task Manager runs none of
    our code). Returns the handle, or None off Windows."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def join_job(job, proc):
    """Put a just-started subprocess (and, by inheritance, everything it will
    start) into the job. Quietly does nothing off Windows or if the job
    couldn't be made."""
    if job is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:
        return False


def _tree(proc):
    """The subprocess and its workers, via psutil when available."""
    try:
        import psutil

        p = psutil.Process(proc.pid)
        return [p] + p.children(recursive=True)
    except Exception:
        return None


def pause_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in procs:
            try:
                p.suspend()
            except Exception:
                pass
        return True
    if not sys.platform.startswith("win"):
        import signal

        os.kill(proc.pid, signal.SIGSTOP)  # no psutil: main process only
        return True
    return False


def resume_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in reversed(procs):
            try:
                p.resume()
            except Exception:
                pass
        return
    if not sys.platform.startswith("win"):
        import signal

        os.kill(proc.pid, signal.SIGCONT)


def kill_tree(proc):
    procs = _tree(proc)
    if procs is not None:
        for p in procs:
            try:
                p.resume()
            except Exception:
                pass
        for p in reversed(procs):  # workers first, then the parent
            try:
                p.terminate()
            except Exception:
                pass
        return
    try:
        resume_tree(proc)
    except Exception:
        pass
    proc.terminate()


# ---------------------------------------------------------------------------
# settings: everything here already has an answer
# ---------------------------------------------------------------------------
DEFAULTS = {
    # the pull
    "pull": False,  # pull off the Odyssey Pro before anything else
    "pull_target": "",  # only observations/targets matching this
    "pull_seestar": False,  # pull off a Seestar (S50 / S50 Pro)
    "seestar_at": "",  # "" = look (USB drive, then \\seestar); else drive/folder/UNC/host
    # the quick look (starstack, if it's installed)
    "quick_look": False,  # after the pull, also stack the archive -- fast and rough
    "restack": False,  # redo sessions that already have a stack (one run, then off)
    "out_dir": "",  # "" = next to the frames (stacked/ or stacks/)
    "stackingwizard": "",  # path to StackingWizard.exe; "" = look in the usual places
    # quick-look stacking knobs, passed through to starstack
    "bits16": True,
    "quality": True,
    "weights": True,
    "align": True,
    "normalize": True,
    "darks": True,
    "report": True,
    "method": "sigma",
    "sigma": 3.0,
    "debayer": "auto",
    "jobs": 0,
    "scratch": "",
}
LABELS = {  # what the main window says when something is off-default
    "bits16": lambda v: None if v else "32-bit",
    "quality": lambda v: None if v else "keep every frame",
    "weights": lambda v: None if v else "no weighting",
    "align": lambda v: None if v else "no alignment",
    "normalize": lambda v: None if v else "no level-match",
    "darks": lambda v: None if v else "no darks",
    "report": lambda v: None if v else "no report",
    "method": lambda v: None if v == "sigma" else v,
    "sigma": lambda v: None if abs(v - 3.0) < 1e-9 else f"sigma {v:g}",
    "debayer": lambda v: None if v == "auto" else f"debayer {v}",
    "jobs": lambda v: None if not v else f"{v} workers",
    "scratch": lambda v: None if not v else f"scratch: {v}",
    "restack": lambda v: "redo already-stacked sessions" if v else None,
    "out_dir": lambda v: None,  # shown in its own row, not the summary
    "pull": lambda v: None,
    "pull_target": lambda v: None,
    "pull_seestar": lambda v: None,
    "seestar_at": lambda v: None,
    "quick_look": lambda v: None,
    "stackingwizard": lambda v: None,
}
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".scopepull-owl.json")
# Where the owl kept his settings when he lived in starstack. Read once, so
# nobody has to re-tick their boxes because the app moved house.
OLD_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".starstack.json")


def load_settings():
    import json

    s = dict(DEFAULTS)
    for path in (SETTINGS_FILE, OLD_SETTINGS_FILE):
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            s.update({k: v for k, v in saved.items() if k in DEFAULTS})
            break
        except Exception:
            continue
    return s


def save_settings(s):
    try:
        import json

        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=2)
    except Exception:
        pass


def settings_to_args(s):
    """The quick-look overrides, as starstack flags. Defaults produce nothing."""
    a = []
    if s["bits16"]:
        a += ["--bits", "16"]
    if not s["quality"]:
        a += ["--keep-all"]
    if not s["weights"]:
        a += ["--no-weights"]
    if not s["align"]:
        a += ["--no-align"]
    if not s["normalize"]:
        a += ["--no-normalize"]
    if not s["darks"]:
        a += ["--no-darks"]
    if s["method"] != "sigma":
        a += ["--method", s["method"]]
    if s.get("restack"):
        a += ["--restack"]
    if abs(float(s["sigma"]) - 3.0) > 1e-9:
        a += ["--sigma", f"{float(s['sigma']):g}"]
    if s["debayer"] != "auto":
        a += ["--debayer", s["debayer"]]
    if s["jobs"]:
        a += ["-j", str(int(s["jobs"]))]
    if s.get("scratch"):
        a += ["--scratch", s["scratch"]]
    return a


def summarize(s):
    parts = [LABELS[k](s[k]) for k in DEFAULTS]
    parts = [p for p in parts if p]
    return ("options: " + ", ".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# the outside world: our own CLI, starstack, StackingWizard, PyPI
# ---------------------------------------------------------------------------
def _run_cli(args, timeout=60):
    """One quick scopepull command, captured. "" on any failure."""
    try:
        return subprocess.run(
            self_cli() + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def probe_roots():
    """{scope: archive root} straight from `scopepull status --json`, so the
    window and the CLI can never disagree about where the nights live."""
    import json

    fallback = {name: os.path.join(os.path.expanduser("~"), "Astro", name) for name in SCOPE_LABEL}
    r = _run_cli(["status", "--json"], timeout=60)
    if r is None or r.returncode != 0:
        return fallback, None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return fallback, None
    roots = {k: os.path.expanduser(v) for k, v in (data.get("roots") or {}).items()}
    for name in SCOPE_LABEL:
        roots.setdefault(name, fallback[name])
    return roots, data


def find_starstack():
    """Path to the starstack command, or None. PATH first; then the places
    pipx, uv and Python's own Scripts folder put console scripts -- because on
    Windows a double-clicked exe inherits Explorer's PATH from sign-in time,
    which doesn't know about a `pipx ensurepath` run this afternoon."""
    import glob
    import shutil

    exe = shutil.which("starstack")
    if exe:
        return exe
    home = os.path.expanduser("~")
    names = ("starstack.exe", "starstack") if sys.platform.startswith("win") else ("starstack",)
    candidates = [
        os.path.join(home, ".local", "bin"),  # pipx / uv default
        os.path.join(os.environ.get("PIPX_BIN_DIR", ""), ""),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "pipx", "venvs", "starstack", "Scripts"),
        os.path.join(home, ".local", "pipx", "venvs", "starstack", "bin"),
        os.path.join(home, "pipx", "venvs", "starstack", "Scripts"),
        os.path.join(os.environ.get("APPDATA", ""), "Python", "Python31*", "Scripts"),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python31*", "Scripts"
        ),
        os.path.join(home, ".local", "share", "uv", "tools", "starstack", "Scripts"),
        os.path.join(home, ".local", "share", "uv", "tools", "starstack", "bin"),
    ]
    for d in candidates:
        if not d.strip(os.sep):
            continue
        for name in names:
            for hit in sorted(glob.glob(os.path.join(d, name)), reverse=True):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    return hit
    return None


def probe_starstack():
    """(version, exe) if starstack is installed, else (None, None). The quick
    look needs it; the pull doesn't. Call it off the UI thread."""
    exe = find_starstack()
    if not exe:
        return None, None
    version = "?"
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW
        )
        text = (out.stdout or out.stderr).strip()
        if text:
            version = text.split()[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return version, exe


def find_system_python(min_version=(3, 10)):
    """A real CPython on this machine (not the one frozen inside the exe --
    that has no pip). Returns the command as a list, or None."""
    import glob
    import shutil

    cands = []
    if sys.platform.startswith("win"):
        py = shutil.which("py")
        if py:
            cands += [[py, "-3.13"], [py, "-3.12"], [py, "-3.11"], [py, "-3"]]
        for pat in (
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Programs",
                "Python",
                "Python31[0-9]*",
                "python.exe",
            ),
            os.path.join(os.environ.get("ProgramFiles", ""), "Python31[0-9]*", "python.exe"),
        ):
            cands += [[p] for p in sorted(glob.glob(pat), reverse=True)]
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p:  # the Store stub opens a shop window, not Python
            cands.append([p])
    if not FROZEN:
        cands.append([sys.executable])
    for cmd in cands:
        try:
            out = subprocess.run(
                cmd + ["-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=NO_WINDOW,
            ).stdout.split()
            if len(out) == 2 and (int(out[0]), int(out[1])) >= min_version:
                return cmd
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def _vtuple(v):
    """'0.2.10' -> (0, 2, 10); anything odd -> () so it never wins a comparison."""
    import re

    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(v or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _fetch_json(url, timeout=3.0):
    """One small GET, no data sent, quiet on any failure (the scope's Wi-Fi has
    no internet, and that's fine)."""
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "scopepull-owl", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def latest_scopepull():
    """Newest scopepull on PyPI, or None if we can't ask."""
    d = _fetch_json("https://pypi.org/pypi/scopepull/json")
    try:
        return d["info"]["version"]
    except (TypeError, KeyError):
        return None


def find_stackingwizard(configured=""):
    """StackingWizard is a portable exe people keep in Downloads or on the
    Desktop. The configured path first; then the usual folders, newest wins."""
    import glob

    if configured and os.path.exists(configured):
        return configured
    home = os.path.expanduser("~")
    places = [
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Astro"),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    hits = []
    for d in places:
        if d and os.path.isdir(d):
            for pat in (
                "StackingWizard*.exe",
                "*/StackingWizard*.exe",
                "stackingwizard*",
                "*/stackingwizard*",
            ):
                hits += [
                    h
                    for h in glob.glob(os.path.join(d, pat))
                    if os.path.isfile(h)
                    and (h.lower().endswith(".exe") or not sys.platform.startswith("win"))
                ]
    hits = [h for h in hits if "uninstall" not in os.path.basename(h).lower()]
    if not hits:
        return None
    return max(hits, key=lambda h: os.path.getmtime(h))


class OptionsDialog(tk.Toplevel):
    """The overrides. The pull has none worth having; these are the quick look."""

    def __init__(self, master, settings, on_close):
        super().__init__(master)
        self.title("scopepull options")
        self.configure(bg=NAVY)
        self.resizable(False, False)
        self.transient(master)
        self.settings, self.on_close = settings, on_close
        self.vars = {}
        pad = {"padx": 18, "pady": 3}

        def section(text):
            tk.Label(self, text=text, fg=MUTE, bg=NAVY, font=(SANS[0], 10, "bold")).pack(
                anchor="w", padx=18, pady=(12, 2)
            )

        def check(key, text):
            v = tk.BooleanVar(value=bool(settings[key]))
            self.vars[key] = v
            tk.Checkbutton(
                self,
                text=text + ("   (default)" if DEFAULTS[key] else ""),
                variable=v,
                bg=NAVY,
                fg="#c9cfe0",
                selectcolor=NAVY2,
                activebackground=NAVY,
                activeforeground=CREAM,
                font=SANS,
                anchor="w",
                highlightthickness=0,
                bd=0,
            ).pack(fill="x", **pad)

        def choice(key, text, options, default_note):
            row = tk.Frame(self, bg=NAVY)
            row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(
                side="left"
            )
            v = tk.StringVar(value=str(settings[key]))
            self.vars[key] = v
            m = tk.OptionMenu(row, v, *options)
            m.config(
                bg=NAVY2,
                fg=CREAM,
                activebackground="#22304a",
                activeforeground=CREAM,
                relief="flat",
                highlightthickness=0,
                font=SANS,
                width=8,
            )
            m["menu"].config(
                bg=NAVY2, fg=CREAM, activebackground="#22304a", activeforeground=CREAM, font=SANS
            )
            m.pack(side="left")
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(
                side="left", padx=(8, 0)
            )

        def number(key, text, default_note):
            row = tk.Frame(self, bg=NAVY)
            row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(
                side="left"
            )
            v = tk.StringVar(value=str(settings[key]))
            self.vars[key] = v
            tk.Entry(
                row,
                textvariable=v,
                width=6,
                bg=NAVY2,
                fg=CREAM,
                insertbackground=CREAM,
                relief="flat",
                font=SANS,
                justify="center",
            ).pack(side="left", ipady=3)
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(
                side="left", padx=(8, 0)
            )

        def folder(key, text, default_note):
            row = tk.Frame(self, bg=NAVY)
            row.pack(fill="x", **pad)
            tk.Label(row, text=text, fg="#c9cfe0", bg=NAVY, font=SANS, width=30, anchor="w").pack(
                side="left"
            )
            v = tk.StringVar(value=str(settings.get(key, "")))
            self.vars[key] = v
            tk.Entry(
                row,
                textvariable=v,
                width=22,
                bg=NAVY2,
                fg=CREAM,
                insertbackground=CREAM,
                relief="flat",
                font=(SANS[0], 9),
            ).pack(side="left", ipady=3)

            def pick():
                d = filedialog.askdirectory(title=text, parent=self)
                if d:
                    v.set(d)

            tk.Button(
                row,
                text="…",
                command=pick,
                bg=NAVY2,
                fg=CREAM,
                activebackground="#22304a",
                activeforeground=CREAM,
                relief="flat",
                padx=6,
                font=SANS,
            ).pack(side="left", padx=(4, 0))
            tk.Label(row, text=default_note, fg=MUTE, bg=NAVY, font=(SANS[0], 9)).pack(
                side="left", padx=(8, 0)
            )

        tk.Label(
            self,
            text="The pull has no options worth having: it takes everything the archive hasn't "
            "got. Everything below is the quick-look stack, and already has an answer.",
            fg=MUTE,
            bg=NAVY,
            font=SANS,
            wraplength=500,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(14, 0))

        section("Frames")
        check("darks", "Use dark frames (found by name, or in a darks/ folder)")
        check("quality", "Drop blurry, cloudy and starved frames")
        check("weights", "Weight the rest by sharpness and noise")
        check("align", "Star-align every frame (off only if they're already registered)")
        check("normalize", "Level-match frames before combining")
        choice(
            "debayer",
            "Bayer pattern",
            ["auto", "RGGB", "BGGR", "GRBG", "GBRG", "none"],
            "default: auto (the owl looks)",
        )

        section("Whole night")
        check("restack", "Redo sessions that already have a stack (one run, then turns itself off)")

        section("Combine")
        choice("method", "How frames are combined", ["sigma", "mean", "median"], "default: sigma")
        number("sigma", "Clip threshold (in MADs, for sigma)", "default: 3.0")
        number("jobs", "Worker processes (0 = let it pick)", "default: 0")
        folder("scratch", "Scratch folder for the temporary cube", "default: system temp (10+ GB)")

        section("Output")
        check("bits16", "16-bit output (off = 32-bit float)")
        check("report", "Write frames.csv with per-frame numbers next to the output")

        section("Hand-off")
        folder("stackingwizard", "StackingWizard.exe", "default: the owl looks for it")

        foot = tk.Frame(self, bg=NAVY)
        foot.pack(fill="x", padx=18, pady=(16, 14))
        tk.Button(
            foot,
            text="Reset to how the owl likes it",
            command=self.reset,
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=10,
            font=SANS,
        ).pack(side="left")
        tk.Button(
            foot,
            text="Done",
            command=self.close,
            bg=RED,
            fg="#fff4ee",
            activebackground=RED_DARK,
            activeforeground="#fff4ee",
            relief="flat",
            padx=18,
            font=(SANS[0], 11, "bold"),
        ).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())

    def reset(self):
        for k in self.vars:
            v = DEFAULTS[k]
            self.vars[k].set(v if not isinstance(self.vars[k], tk.StringVar) else str(v))

    def collect(self):
        s = dict(self.settings)
        for k, var in self.vars.items():
            val = var.get()
            if isinstance(DEFAULTS[k], bool):
                s[k] = bool(val)
            elif isinstance(DEFAULTS[k], float):
                try:
                    s[k] = float(val)
                except ValueError:
                    s[k] = DEFAULTS[k]
            elif isinstance(DEFAULTS[k], int):
                try:
                    s[k] = max(0, int(float(val)))
                except ValueError:
                    s[k] = DEFAULTS[k]
            else:
                s[k] = str(val)
        return s

    def close(self):
        self.on_close(self.collect())
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("scopepull")
        self.configure(bg=NAVY)
        self._set_icon()
        self.geometry("960x800")
        self.minsize(760, 620)
        self.proc = None
        self.paused = False
        self.q = queue.Queue()
        self.preview_path = None
        self.out_dir = None
        self._pulled_this_run = False
        self._stacked_this_run = False
        self._stopping = False
        self._last_rc = 0
        self.settings = load_settings()
        self.roots = {
            name: os.path.join(os.path.expanduser("~"), "Astro", name) for name in SCOPE_LABEL
        }
        self.starstack = (None, None)  # (version, exe)
        self.job = make_job()  # dies with us; takes every child along
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(80, self._pump)

    def _set_icon(self):
        """The owl in the title bar and taskbar."""
        ico = os.path.join(ASSETS, "scopepull.ico")
        png = os.path.join(ASSETS, "icon_256.png")
        try:
            if sys.platform.startswith("win") and os.path.exists(ico):
                self.iconbitmap(default=ico)
            elif os.path.exists(png):
                self._icon_img = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    # ---- layout -------------------------------------------------------------
    def _build(self):
        top = tk.Frame(self, bg=NAVY)
        top.pack(fill="x", padx=18, pady=(14, 6))
        # the owl has degrees of grumpy. he does not have a smile.
        self.faces = {}
        for name in ("grumpy", "focused", "waiting", "judging", "grudging", "furious", "blink"):
            p = os.path.join(ASSETS, "faces", f"owl_{name}.png")
            if os.path.exists(p):
                try:
                    self.faces[name] = tk.PhotoImage(file=p)
                except tk.TclError:
                    pass
        if not self.faces:
            owl_path = os.path.join(ASSETS, "owl_256.png")
            if os.path.exists(owl_path):
                try:
                    self.faces["grumpy"] = tk.PhotoImage(file=owl_path).subsample(2, 2)
                except tk.TclError:
                    pass
        self.face_lbl = tk.Label(top, bg=NAVY)
        self.face_lbl.pack(side="left", padx=(0, 14))
        self.face = None
        self._blink_job = None
        self.set_face("grumpy")
        words = tk.Frame(top, bg=NAVY)
        words.pack(side="left", fill="x", expand=True)
        tk.Label(words, text="scopepull", fg=CREAM, bg=NAVY, font=(SANS[0], 30, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            words, text="Gets the night off your smart scope.", fg="#c9cfe0", bg=NAVY, font=SANS
        ).pack(anchor="w")
        # the version, where people look when they wonder if they're current.
        # turns into the update notice when they aren't.
        self.ver_lbl = tk.Label(
            words, text=f"v{__version__}", fg=MUTE, bg=NAVY, font=(SANS[0], 9), cursor="arrow"
        )
        self.ver_lbl.pack(anchor="w")
        self.mode_lbl = tk.Label(
            words, text="", fg=MUTE, bg=NAVY, font=MONO, justify="left", wraplength=520
        )
        self.mode_lbl.pack(anchor="w", pady=(6, 0))

        self.button = Button(top, size=150, command=self.press)
        self.button.pack(side="right")

        row = tk.Frame(self, bg=NAVY)
        row.pack(fill="x", padx=18, pady=(4, 8))
        tk.Label(row, text="Folder", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.folder = tk.StringVar()
        self.folder.trace_add("write", lambda *_: (self._describe(), self._folder_reindex()))
        self.folder_entry = tk.Entry(
            row,
            textvariable=self.folder,
            bg=NAVY2,
            fg=CREAM,
            insertbackground=CREAM,
            relief="flat",
            font=MONO,
        )
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._hint_bind(self.folder_entry, self.folder)
        tk.Button(
            row,
            text="Browse…",
            command=self.browse,
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=12,
            font=SANS,
        ).pack(side="left", padx=(8, 0))

        # the Odyssey row: its API lives in this app, so this box is never
        # greyed out. Whether the scope answers is the pull's business.
        srow = tk.Frame(self, bg=NAVY)
        srow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(srow, text="Odyssey", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.pull_var = tk.BooleanVar(value=bool(self.settings.get("pull")))
        self.pull_chk = tk.Checkbutton(
            srow,
            text="Pull new observations off the Odyssey Pro",
            variable=self.pull_var,
            bg=NAVY,
            fg="#c9cfe0",
            selectcolor=NAVY2,
            activebackground=NAVY,
            activeforeground=CREAM,
            font=SANS,
            highlightthickness=0,
            bd=0,
            command=self._pull_changed,
        )
        self.pull_chk.pack(side="left")
        tk.Label(srow, text="target", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(14, 6))
        self.pull_target = tk.StringVar(value=self.settings.get("pull_target", ""))
        self.pull_target.trace_add("write", lambda *_: self._pull_changed())
        self.target_entry = tk.Entry(
            srow,
            textvariable=self.pull_target,
            width=12,
            bg=NAVY2,
            fg=CREAM,
            insertbackground=CREAM,
            relief="flat",
            font=MONO,
        )
        self.target_entry.pack(side="left", ipady=4)
        self._hint_bind(self.target_entry, self.pull_target)
        shint = tk.Frame(self, bg=NAVY)
        shint.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(shint, text="", bg=NAVY, font=SANS, width=7).pack(side="left", padx=(0, 8))
        self.scope_lbl = tk.Label(
            shint, text="reading the archive…", fg=MUTE, bg=NAVY, font=(SANS[0], 9), anchor="w"
        )
        self.scope_lbl.pack(side="left", fill="x", expand=True)
        threading.Thread(target=self._probe_self, daemon=True).start()
        threading.Thread(target=self._check_version, daemon=True).start()

        # the Seestar row: an S50 / S50 Pro is a folder (USB drive, or the guest
        # share \\seestar on Wi-Fi in station mode). The box is never greyed
        # out: the scope may be switched on after the owl was.
        zrow = tk.Frame(self, bg=NAVY)
        zrow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(zrow, text="Seestar", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.seestar_var = tk.BooleanVar(value=bool(self.settings.get("pull_seestar")))
        self.seestar_chk = tk.Checkbutton(
            zrow,
            text="Pull new subs off the Seestar",
            variable=self.seestar_var,
            bg=NAVY,
            fg="#c9cfe0",
            selectcolor=NAVY2,
            activebackground=NAVY,
            activeforeground=CREAM,
            font=SANS,
            highlightthickness=0,
            bd=0,
            command=self._seestar_changed,
        )
        self.seestar_chk.pack(side="left")
        tk.Label(zrow, text="at", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(14, 6))
        self.seestar_at = tk.StringVar(value=self.settings.get("seestar_at", ""))
        self.seestar_at.trace_add("write", lambda *_: self._seestar_changed())
        self.seestar_entry = tk.Entry(
            zrow,
            textvariable=self.seestar_at,
            width=18,
            bg=NAVY2,
            fg=CREAM,
            insertbackground=CREAM,
            relief="flat",
            font=MONO,
        )
        self.seestar_entry.pack(side="left", ipady=4)
        self._hint_bind(self.seestar_entry, self.seestar_at)
        zhint = tk.Frame(self, bg=NAVY)
        zhint.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(zhint, text="", bg=NAVY, font=SANS, width=7).pack(side="left", padx=(0, 8))
        self.seestar_lbl = tk.Label(
            zhint, text="looking for a Seestar…", fg=MUTE, bg=NAVY, font=(SANS[0], 9), anchor="w"
        )
        self.seestar_lbl.pack(side="left", fill="x", expand=True)
        self.seestar_found = None  # (how, MyWorks path) once the probe answers
        threading.Thread(target=self._probe_seestar, daemon=True).start()

        orow = tk.Frame(self, bg=NAVY)
        orow.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(orow, text="Output", fg=MUTE, bg=NAVY, font=SANS).pack(side="left", padx=(0, 8))
        self.out_var = tk.StringVar(value=self.settings.get("out_dir", ""))
        self.out_entry = tk.Entry(
            orow,
            textvariable=self.out_var,
            bg=NAVY2,
            fg=CREAM,
            insertbackground=CREAM,
            relief="flat",
            font=MONO,
        )
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(
            orow,
            text="Browse…",
            command=self.browse_out,
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=12,
            font=SANS,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            orow,
            text="Clear",
            command=lambda: self.out_var.set(""),
            bg=NAVY2,
            fg="#c9cfe0",
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=8,
            font=SANS,
        ).pack(side="left", padx=(6, 0))
        self.out_var.trace_add("write", lambda *_: self._out_changed())
        self.out_entry.bind("<FocusIn>", lambda e: self._out_placeholder(False))
        self.out_entry.bind("<FocusOut>", lambda e: self._out_placeholder(True))
        self._out_placeholder(True)

        opts = tk.Frame(self, bg=NAVY)
        opts.pack(fill="x", padx=18)
        tk.Button(
            opts,
            text="Options…",
            command=self.open_options,
            bg=NAVY2,
            fg="#c9cfe0",
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=10,
            font=SANS,
        ).pack(side="left")
        self.opts_lbl = tk.Label(opts, text=summarize(self.settings), fg=MUTE, bg=NAVY, font=MONO)
        self.opts_lbl.pack(side="left", padx=(12, 0))
        # the whole log to the clipboard, for pasting at whoever is helping you
        tk.Button(
            opts,
            text="Copy log",
            command=self.copy_log,
            bg=NAVY2,
            fg="#c9cfe0",
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=10,
            font=SANS,
        ).pack(side="right")
        # the quick look is starstack's job: a rough stack to see if the night
        # was worth keeping. The real stack is StackingWizard, below.
        # Packed on the right FIRST so a long hint can never push it off the
        # edge of the window (starstack 0.2.11: it was only visible full-screen).
        self.install_btn = tk.Button(
            opts,
            text="Install starstack…",
            command=self.install_starstack,
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=8,
            font=(SANS[0], 9),
        )  # shown only when needed
        self.quick_var = tk.BooleanVar(value=bool(self.settings.get("quick_look")))
        self.quick_chk = tk.Checkbutton(
            opts,
            text="quick-look stack after the pull (starstack's, fast and rough)",
            variable=self.quick_var,
            bg=NAVY,
            fg="#c9cfe0",
            selectcolor=NAVY2,
            activebackground=NAVY,
            activeforeground=CREAM,
            font=(SANS[0], 10),
            highlightthickness=0,
            bd=0,
            state="disabled",
            command=self._quick_changed,
        )
        self.quick_chk.pack(side="right", padx=(0, 14))
        threading.Thread(target=self._probe_starstack, daemon=True).start()

        foot = tk.Frame(self, bg=NAVY)
        foot.pack(side="bottom", fill="x", padx=18, pady=(0, 12))
        body = tk.PanedWindow(self, orient="vertical", bg=NAVY, sashwidth=6, sashrelief="flat")
        body.pack(fill="both", expand=True, padx=18, pady=(6, 10))
        logbox = tk.Frame(body, bg=INK)
        self.log = tk.Text(
            logbox,
            bg=INK,
            fg="#d5dae6",
            insertbackground=CREAM,
            relief="flat",
            font=MONO,
            wrap="word",
            state="disabled",
            padx=10,
            pady=8,
            cursor="arrow",
            takefocus=1,
            height=6,
        )
        sb = tk.Scrollbar(
            logbox,
            orient="vertical",
            command=self.log.yview,
            bg=NAVY2,
            troughcolor=INK,
            activebackground="#22304a",
            relief="flat",
            width=14,
        )
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("owl", foreground="#f0cf96")
        self.log.tag_configure("bad", foreground="#ff8a7a")
        self.log.tag_configure("good", foreground="#9be59b")
        # keyboard: click the log (or Tab to it) and use the arrows / PgUp / PgDn /
        # Home / End. A disabled Text ignores keys, so drive the view ourselves.
        keys = {
            "<Up>": ("scroll", -1, "units"),
            "<Down>": ("scroll", 1, "units"),
            "<Prior>": ("scroll", -1, "pages"),
            "<Next>": ("scroll", 1, "pages"),
            "<Home>": ("moveto", 0.0),
            "<End>": ("moveto", 1.0),
        }

        def _nav(action):
            def handler(_event):
                if isinstance(self.focus_get(), tk.Entry):
                    return None  # typing a path: leave the keys alone
                self.log.yview(*action)
                return "break"

            return handler

        for key, action in keys.items():
            self.bind_all(key, _nav(action))
        self.log.bind("<Button-1>", lambda e: self.log.focus_set())
        body.add(logbox, minsize=90)
        # below the log: the archive on the left (what's been pulled, by target
        # and night), the quick-look picture on the right
        lower = tk.PanedWindow(body, orient="horizontal", bg=NAVY, sashwidth=6, sashrelief="flat")
        from tkinter import ttk

        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(
            "Owl.Treeview",
            background=INK,
            fieldbackground=INK,
            foreground="#d5dae6",
            rowheight=22,
            font=(SANS[0], 10),
            borderwidth=0,
        )
        st.configure(
            "Owl.Treeview.Heading",
            background=NAVY2,
            foreground=CREAM,
            font=(SANS[0], 10, "bold"),
            relief="flat",
        )
        st.map(
            "Owl.Treeview", background=[("selected", "#2d3f63")], foreground=[("selected", CREAM)]
        )
        abox = tk.Frame(lower, bg=INK)
        self.tree = ttk.Treeview(
            abox,
            style="Owl.Treeview",
            columns=("night", "frames", "time", "pulled"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="target", anchor="w")
        self.tree.heading("night", text="night")
        self.tree.heading("frames", text="frames")
        self.tree.heading("time", text="integration")
        self.tree.heading("pulled", text="pulled")
        self.tree.column("#0", width=190, stretch=True)
        self.tree.column("night", width=96, anchor="w", stretch=False)
        self.tree.column("frames", width=64, anchor="e", stretch=False)
        self.tree.column("time", width=92, anchor="e", stretch=False)
        self.tree.column("pulled", width=92, anchor="w", stretch=False)
        tsb = tk.Scrollbar(
            abox,
            orient="vertical",
            command=self.tree.yview,
            bg=NAVY2,
            troughcolor=INK,
            activebackground="#22304a",
            relief="flat",
            width=14,
        )
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selection_changed())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.tag_configure("target", foreground=CREAM, font=(SANS[0], 10, "bold"))
        self.tree.tag_configure("fresh", foreground="#9be59b")
        self.tree.tag_configure("empty", foreground=MUTE)
        lower.add(abox, minsize=320)
        self.preview_frame = tk.Frame(lower, bg=INK)
        self.preview_lbl = tk.Label(
            self.preview_frame,
            bg=INK,
            fg=MUTE,
            font=SANS,
            text="the quick look shows up here",
            wraplength=260,
            justify="center",
        )
        self.preview_lbl.pack(fill="both", expand=True)
        lower.add(self.preview_frame, minsize=160)
        body.add(lower, minsize=140)
        self.lower = lower
        self.after(200, lambda: lower.sash_place(0, max(380, int(lower.winfo_width() * 0.58)), 0))
        self.body = body
        self.archive_rows = []
        self.archive_root = None
        # log on top, picture below, roughly half and half once it has a size
        self.after(150, lambda: body.sash_place(0, 0, max(140, int(body.winfo_height() * 0.40))))
        self._resize_job = None
        self.preview_frame.bind("<Configure>", self._on_resize)

        # what happens to a night once it's on this computer: a real stacker.
        # pick a night in the table (or a target: every night of it) and hand it over.
        self.sw_btn = tk.Button(
            foot,
            text="Open in StackingWizard",
            command=self.open_stackingwizard,
            state="disabled",
            bg="#2f6b3a",
            fg=CREAM,
            activebackground="#3a8449",
            activeforeground=CREAM,
            relief="flat",
            padx=12,
            font=SANS,
        )
        self.sw_btn.pack(side="left")
        self.open_btn = tk.Button(
            foot,
            text="Open folder",
            command=self.open_folder,
            state="disabled",
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=12,
            font=SANS,
        )
        self.open_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = tk.Button(
            foot,
            text="Stop",
            command=self.stop,
            state="disabled",
            bg=NAVY2,
            fg=CREAM,
            activebackground="#22304a",
            activeforeground=CREAM,
            relief="flat",
            padx=12,
            font=SANS,
        )
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status = tk.Label(foot, text="", fg=MUTE, bg=NAVY, font=SANS)
        self.status.pack(side="right")
        self.sw_exe = find_stackingwizard(self.settings.get("stackingwizard", ""))

        self.button.set_label(self.button_label())
        self._refresh_hints()
        self.say(
            "tick your scope and press the button: the night lands in the archive below. Pick a "
            "night there and send it to a stacker. That's the entire manual.\n",
            "owl",
        )
        self.after(300, self.refresh_archive)
        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            # a folder was dropped on the exe: that IS the button press
            self.folder.set(sys.argv[1])
            self.after(600, self.press)

    # ---- the face -----------------------------------------------------------
    def set_face(self, name):
        img = self.faces.get(name) or self.faces.get("grumpy")
        if img is not None:
            self.face_lbl.config(image=img)
        if name != "blink":
            self.face = name
            self._face_seq = getattr(self, "_face_seq", 0) + 1

    def _blink(self):
        """Every few seconds while working, so he reads as alive, not a sticker."""
        self._blink_job = None
        running = self.proc is not None and self.proc.poll() is None
        if not running or self.paused or self.face == "waiting" or "blink" not in self.faces:
            return
        current = self.face
        self.set_face("blink")
        self.after(110, lambda: self.set_face(current))
        self._schedule_blink()

    def _schedule_blink(self):
        import random

        if self._blink_job:
            self.after_cancel(self._blink_job)
        self._blink_job = self.after(random.randint(2500, 6000), self._blink)

    # ---- grey hints in empty boxes ------------------------------------------
    # What an empty box means, written in the box. The StringVar carries the
    # hint text while it shows, so readers go through _real() to get "".
    def _hint_bind(self, entry, var):
        entry._hint_on = False
        entry.bind("<FocusIn>", lambda e: self._hint_clear(entry, var))
        entry.bind("<FocusOut>", lambda e: self._refresh_hints())

    def _hint_clear(self, entry, var):
        if getattr(entry, "_hint_on", False):
            self._hinting = True
            entry._hint_on = False
            var.set("")
            entry.config(fg=CREAM)
            self._hinting = False

    def _hint_set(self, entry, var, text):
        """Show `text` greyed if the box is empty and not being typed in."""
        self._hinting = True
        try:
            if getattr(entry, "_hint_on", False):
                entry._hint_on = False
                var.set("")
            if text and not var.get().strip() and self.focus_get() is not entry:
                entry._hint_on = True
                entry._hint_text = text
                entry.config(fg=MUTE)
                var.set(text)
            else:
                entry.config(fg=CREAM)
        finally:
            self._hinting = False

    def _real(self, entry, var):
        """The box's real value: "" while it only shows its hint. A value set
        from code (a folder dropped on the exe, a test) while the hint was up
        counts as real and switches the hint off."""
        if getattr(entry, "_hint_on", False):
            if var.get() == getattr(entry, "_hint_text", None):
                return ""
            entry._hint_on = False
            entry.config(fg=CREAM)
        return var.get().strip().strip('"')

    def _refresh_hints(self):
        if not hasattr(self, "target_entry"):
            return
        scope = self.pulling()
        if scope:
            self._hint_set(
                self.folder_entry,
                self.folder,
                f"empty = the {scope} archive, {self.roots[scope]}   (or a folder to pull into)",
            )
            self._hint_set(self.target_entry, self.pull_target, "all targets")
        else:
            self._hint_set(
                self.folder_entry,
                self.folder,
                "a folder of frames, or a folder of session folders",
            )
            self._hint_set(self.target_entry, self.pull_target, "all targets")
        if hasattr(self, "seestar_entry"):
            self._hint_set(self.seestar_entry, self.seestar_at, "USB, then \\\\seestar")

    # ---- what this app knows about itself -----------------------------------
    def _probe_self(self):
        self.q.put(("roots", probe_roots()))

    def _check_version(self):
        latest = latest_scopepull()
        if latest and _vtuple(latest) > _vtuple(__version__):
            self.q.put(("version_latest", latest))

    def _roots_found(self, payload):
        roots, data = payload
        self.roots = roots
        line = f"Odyssey archive: {roots['odyssey']}"
        if data:
            n = data.get("observations")
            if n is not None:
                line += f"  ·  {n} observation(s) pulled so far"
        self.scope_lbl.config(text=line)
        self._refresh_hints()
        self._describe()
        self.refresh_archive()

    def _version_latest(self, latest):
        self.say(f"psst: scopepull {latest} is out (this is {__version__}). {RELEASES}\n", "owl")
        self.ver_lbl.config(
            text=f"v{__version__}  ·  {latest} is out. Click to get it.",
            fg="#f0cf96",
            cursor="hand2",
        )
        self.ver_lbl.bind("<Button-1>", lambda e: self._open_url(RELEASES))

    def _open_url(self, url):
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            self.say(f"{url}\n", "owl")

    # ---- starstack: the optional quick look ---------------------------------
    NO_STARSTACK = (
        "quick-look stacking needs starstack (the owl's own stacker). Without it this is a "
        "puller, which is most of the point. The button installs it."
    )

    def _probe_starstack(self):
        self.q.put(("starstack", probe_starstack()))

    def _starstack_found(self, found):
        self.starstack = found
        version, exe = found
        if version is None:
            self.quick_chk.config(state="disabled", text="quick-look stack (needs starstack)")
            self.install_btn.pack(side="right", padx=(10, 0))
            if self.quick_var.get():
                self.quick_var.set(False)
                self.settings["quick_look"] = False
        else:
            self.install_btn.pack_forget()
            self.install_btn.config(text="Install starstack…")
            self.quick_chk.config(
                state="normal",
                text=f"quick-look stack after the pull (starstack {version}, fast and rough)",
            )
        if not self.running():
            self.button.set_label(self.button_label())
        self._describe()

    def install_starstack(self):
        """One click: pip-install starstack from GitHub with a Python on this
        machine, then look again. No Python? Ask winget for one."""
        if self.running():
            self.say("busy. Install it after the pull.\n", "bad")
            return
        self.install_btn.config(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._cr_pending = False
        verb = "updating" if self.starstack[0] else "installing"
        self.say(
            f"{verb} starstack (the quick-look stacker). It needs a Python 3.10 or newer on this "
            f"machine -- looking.\n",
            "owl",
        )
        threading.Thread(target=self._install_starstack, daemon=True).start()

    def _install_starstack(self):
        py = find_system_python()
        if py is None and sys.platform.startswith("win"):
            import shutil

            winget = shutil.which("winget")
            if winget:
                self.q.put(
                    (
                        "line",
                        "no Python 3.10+ here. Asking winget to install Python 3.12 -- a minute or two.\n",
                    )
                )
                rc = self._stream(
                    [
                        winget,
                        "install",
                        "--id",
                        "Python.Python.3.12",
                        "-e",
                        "--silent",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                    ]
                )
                if rc == 0:
                    py = find_system_python()
        if py is None:
            self.q.put(
                (
                    "line",
                    "no Python 3.10+ on this machine and no way to fetch one. Get it from python.org "
                    "(tick 'Add python.exe to PATH'), then press Install again.\n",
                )
            )
            self._open_url("https://www.python.org/downloads/windows/")
            self.q.put(("install_done", 1))
            return
        self.q.put(("line", f"using {' '.join(py)}\n"))
        rc = self._stream(
            py
            + [
                "-m",
                "pip",
                "install",
                "--user",
                "--upgrade",
                "--no-warn-script-location",
                f"git+{STARSTACK_REPO}",
            ]
        )
        self.q.put(("install_done", rc))

    def _stream(self, cmd):
        """Run a helper command, its output into the log; return its exit code."""
        try:
            env = dict(os.environ, PYTHONUNBUFFERED="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
            if not sys.platform.startswith("win"):
                env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"  # Debian's "externally managed" refusal
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=NO_WINDOW,
                env=env,
            )
            join_job(self.job, p)
        except OSError as e:
            self.q.put(("line", f"couldn't run {cmd[0]}: {e}\n"))
            return 1
        self._relay(p.stdout, prefix="  ")
        return p.wait()

    def _install_done(self, rc):
        self.install_btn.config(state="normal")
        if rc == 0:
            self.say("installed. Looking for it again.\n", "owl")
            threading.Thread(target=self._probe_starstack, daemon=True).start()
        else:
            self.say(
                f"that didn't work (exit {rc}). The lines above say why. Fallback, in PowerShell: "
                f"pipx install git+{STARSTACK_REPO}\n",
                "bad",
            )

    # ---- the scopes ---------------------------------------------------------
    def _pull_changed(self, save=True):
        if getattr(self, "_hinting", False):
            return
        if self.pull_var.get() and self.seestar_var.get():
            self.seestar_var.set(False)  # one scope per press
            self.settings["pull_seestar"] = False
        self.settings["pull"] = bool(self.pull_var.get())
        self.settings["pull_target"] = self._real(self.target_entry, self.pull_target)
        if save:
            save_settings(self.settings)
        if not self.running():
            self.button.set_label(self.button_label())
        self._refresh_hints()
        self._describe()
        self.refresh_archive()

    def _seestar_changed(self, save=True):
        if getattr(self, "_hinting", False) or not hasattr(self, "seestar_var"):
            return
        if self.seestar_var.get() and self.pull_var.get():
            self.pull_var.set(False)  # one scope per press
            self.settings["pull"] = False
        self.settings["pull_seestar"] = bool(self.seestar_var.get())
        at = self._real(self.seestar_entry, self.seestar_at)
        if at != self.settings.get("seestar_at", ""):
            self.settings["seestar_at"] = at
            if getattr(self, "_seestar_probe_job", None):
                self.after_cancel(self._seestar_probe_job)
            self.seestar_lbl.config(text="looking for a Seestar…")
            self._seestar_probe_job = self.after(
                900, lambda: threading.Thread(target=self._probe_seestar, daemon=True).start()
            )
        if save:
            save_settings(self.settings)
        if not self.running():
            self.button.set_label(self.button_label())
        self._refresh_hints()
        self._describe()
        self.refresh_archive()

    def _probe_seestar(self):
        """Ask our own CLI where the scope is: `scopepull find --scope seestar`
        prints 'found <label> <how>' and exits 0, or exits 3 with the reason."""
        at = self.settings.get("seestar_at", "") or ""
        args = ["find", "--scope", "seestar"]
        if at:
            args += ["--at", at]
        r = _run_cli(args, timeout=90)
        if r is None:
            self.q.put(("seestar", (None, "couldn't ask")))
            return
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out.lower().startswith("found"):
            # "found ZWO Seestar S50 / S50 Pro on USB, drive D:"
            how = out[len("found") :].strip()
            for label in SCOPE_LABEL.values():
                if how.startswith(label):
                    how = how[len(label) :].strip()
            self.q.put(("seestar", (how, None)))
        else:
            why = ((r.stderr or "").strip().splitlines() or ["not found"])[0]
            self.q.put(("seestar", (None, why.split(":", 1)[-1].strip() or "not found")))

    def _seestar_probed(self, payload):
        how, why = payload
        if how:
            self.seestar_found = how
            self.seestar_lbl.config(text=f"Seestar {how}  ·  archive: {self.roots['seestar']}")
        else:
            self.seestar_found = None
            self.seestar_lbl.config(
                text=f"no Seestar right now ({why}). USB, or station mode on the same Wi-Fi."
            )

    def pulling(self):
        """Is the next press a pull, and off which scope? "odyssey", "seestar"
        or None (a folder: stack it)."""
        if bool(self.pull_var.get()):
            return "odyssey"
        if bool(getattr(self, "seestar_var", None) and self.seestar_var.get()):
            return "seestar"
        return None

    def button_label(self):
        p = self.pulling()
        if p:
            return "PULL+STACK" if self.quick_ok() else "PULL"
        # No scope ticked. "STACK" would be a promise we can't keep without
        # starstack, so the button keeps the app's own verb and the owl says
        # what's missing when it's pressed.
        return "STACK" if self.starstack[0] else "PULL"

    def quick_ok(self):
        return bool(self.settings.get("quick_look")) and self.starstack[0] is not None

    def _quick_changed(self):
        self.settings["quick_look"] = bool(self.quick_var.get())
        save_settings(self.settings)
        if not self.running():
            self.button.set_label(self.button_label())
        self._describe()

    # ---- output folder ------------------------------------------------------
    PLACEHOLDER = "next to the frames  (stacked\\ for a session, stacks\\ for a night)"

    def _out_placeholder(self, show):
        """Grey hint inside the empty box; real text otherwise."""
        v = self.out_var.get()
        if show and not v.strip():
            self._showing_hint = True
            self.out_entry.config(fg=MUTE)
            self.out_var.set(self.PLACEHOLDER)
        elif not show and getattr(self, "_showing_hint", False):
            self._showing_hint = False
            self.out_var.set("")
            self.out_entry.config(fg=CREAM)

    def _out_changed(self):
        v = self.out_var.get()
        if v == self.PLACEHOLDER:
            return
        self.out_entry.config(fg=CREAM)
        self._showing_hint = False
        self.settings["out_dir"] = v.strip().strip('"')
        save_settings(self.settings)
        if not v.strip() and self.focus_get() is not self.out_entry:
            self.after(10, lambda: self._out_placeholder(True))

    def chosen_out_dir(self):
        v = self.settings.get("out_dir", "").strip()
        return v if v and v != self.PLACEHOLDER else ""

    def browse_out(self):
        d = filedialog.askdirectory(
            title="Where the stacks go (leave empty for next to the frames)"
        )
        if d:
            self._showing_hint = False
            self.out_entry.config(fg=CREAM)
            self.out_var.set(d)

    # ---- options ------------------------------------------------------------
    def open_options(self):
        def done(s):
            self.settings = s
            save_settings(s)
            self.opts_lbl.config(text=summarize(s))
            self.sw_exe = find_stackingwizard(s.get("stackingwizard", ""))

        OptionsDialog(self, self.settings, done)

    # ---- behaviour ----------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(title="Folder of frames, or a folder of session folders")
        if d:
            self.folder.set(d)

    def _describe(self):
        if getattr(self, "_hinting", False):
            return
        f = self._real(self.folder_entry, self.folder)
        scope = self.pulling()
        then = (
            "then quick-look stack it (nights already stacked are skipped)"
            if self.quick_ok()
            else "then list the archive. Stacking is the next button."
        )
        if scope == "seestar":
            at = (
                self._real(self.seestar_entry, self.seestar_at)
                or "wherever it is (USB, then \\\\seestar)"
            )
            self.mode_lbl.config(
                text=f"copy new subs off the Seestar at {at} into "
                f"{f or self.roots['seestar']}, {then}"
            )
            return
        if scope == "odyssey":
            tgt = self._real(self.target_entry, self.pull_target)
            what = f"new {tgt!r} observations" if tgt else "everything new"
            self.mode_lbl.config(
                text=f"pull {what} off the Odyssey into {f or self.roots['odyssey']}, {then}"
            )
            return
        if not os.path.isdir(f):
            self.mode_lbl.config(
                text="" if self.starstack[0] else "tick a scope above. (No starstack: no stacking.)"
            )
            return
        if is_night(f):
            n = len(night_sessions(f))
            self.mode_lbl.config(
                text=f"whole night: {n} session folders. One press does all of them."
            )
        elif is_sorted(f):
            ld = os.path.join(f, "lights" if os.path.isdir(os.path.join(f, "lights")) else "light")
            n = sum(1 for x in os.listdir(ld) if os.path.splitext(x)[1].lower() in IMAGE_EXT)
            self.mode_lbl.config(text=f"sorted layout, {n} lights. Output: {target_name(f)}.tif")
        elif has_images(f):
            n = sum(1 for x in os.listdir(f) if os.path.splitext(x)[1].lower() in IMAGE_EXT)
            self.mode_lbl.config(text=f"{n} image files. Output: {target_name(f)}.tif")
        else:
            self.mode_lbl.config(text="no images in there.")

    def copy_log(self):
        text = self.log.get("1.0", "end-1c")
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            n = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            self.status.config(text=f"copied {n} lines. Paste them at someone.")
        except tk.TclError:
            self.status.config(text="couldn't reach the clipboard.")

    def _split(self, log_share, animate=False):
        """How the window is shared between log and picture. The log leads while
        the owl works; the picture takes the stage once it exists."""

        def place():
            h = self.body.winfo_height()
            if h > 50:
                self.body.sash_place(0, 0, max(100, int(h * log_share)))
                self.log.yview_moveto(1.0)  # keep the last lines in view in the strip

        self.after(30 if animate else 0, place)

    def say(self, text, tag=None):
        """Append to the log the way a terminal would: `\\n` ends the line, `\\r`
        means the next text overwrites the current line. That's how scopepull's
        "building 187/364 frames" and starstack's "combining rows 400/1094"
        update in place instead of scrolling the window off the bottom."""
        body = text.rstrip("\r\n")
        term = text[len(body) :]
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        if body:
            if getattr(self, "_cr_pending", False):
                self.log.delete("end-1c linestart", "end-1c")  # overwrite the current line
            self.log.insert("end-1c", body, tag)
            self._cr_pending = False
        if "\n" in term:
            self.log.insert("end-1c", "\n")
            self._cr_pending = False
        elif "\r" in term:
            self._cr_pending = True
        if at_bottom:  # follow the log unless the reader scrolled up
            self.log.see("end")
        self.log.configure(state="disabled")

    def _relay(self, stream, prefix=""):
        """Read a child's stdout in bytes (text mode would turn every `\\r` into
        `\\n` and defeat say()), one segment per `\\n` or `\\r`, onto the queue."""
        import codecs

        dec = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
            ch = dec.decode(chunk)
            if not ch:
                continue
            buf += ch
            if ch in "\n\r":
                self.q.put(("line", (prefix + buf) if buf.strip() else buf))
                buf = ""
        buf += dec.decode(b"", final=True)
        if buf:
            self.q.put(("line", prefix + buf + "\n"))

    # ---- the press ----------------------------------------------------------
    def press(self):
        """The one button. A pull, then optionally a quick-look stack of what
        landed; or, with no scope ticked, just a stack of the chosen folder."""
        if self.proc and self.proc.poll() is None:  # running: the button pauses / resumes
            self.toggle_pause()
            return
        f = self._real(self.folder_entry, self.folder)
        scope = self.pulling()
        if not scope and not os.path.isdir(f):
            self.say("tick a scope, or pick a folder. I can't pull a feeling.\n", "bad")
            return
        if not scope and self.starstack[0] is None:
            self.say(self.NO_STARSTACK + "\n", "bad")
            return
        chosen = self.chosen_out_dir()
        if chosen and not os.path.isdir(chosen):
            try:
                os.makedirs(chosen, exist_ok=True)
            except OSError as e:
                self.say(f"can't create the output folder {chosen}: {e}\n", "bad")
                return

        steps = []
        if scope:
            archive = f or self.roots[scope]
            cmd = self_cli() + ["pull", "--scope", scope]
            tgt = self._real(self.target_entry, self.pull_target)
            if tgt:
                cmd += ["--target", tgt]
            if scope == "seestar":
                at = self._real(self.seestar_entry, self.seestar_at)
                if at:
                    cmd += ["--at", at]
            if f:
                cmd += ["--dest", f]
            steps.append(("pull", cmd))
            stack_folder = archive
            self.out_dir = chosen or os.path.join(archive, "stacks")
            self.preview_path = None
        else:
            stack_folder = f
            if is_night(f):
                self.out_dir = chosen or os.path.join(f, "stacks")
                self.preview_path = None
            elif is_session(f):
                self.out_dir = chosen or os.path.join(f, "stacked")
                os.makedirs(self.out_dir, exist_ok=True)
                name = target_name(f)
                self.preview_path = os.path.join(self.out_dir, name + "_look.png")
            else:
                self.say("no image files in that folder.\n", "bad")
                return

        self._pulled_this_run = bool(scope)
        self._stacked_this_run = (not scope) or self.quick_ok()
        if self._stacked_this_run:
            out_arg = (
                os.path.join(self.out_dir, target_name(stack_folder) + ".tif")
                if self.preview_path
                else self.out_dir
            )
            cmd = [self.starstack[1], stack_folder, "-o", out_arg]
            if self.preview_path:
                cmd += ["--preview", self.preview_path]
            cmd += settings_to_args(self.settings)
            if self.settings["report"]:
                cmd += (
                    ["--report", os.path.join(self.out_dir, "frames.csv")]
                    if self.preview_path
                    else ["--report", "x"]
                )
            steps.append(("stack", cmd))

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._cr_pending = False
        if self._stacked_this_run:
            self.preview_lbl.config(image="", text="working…")
        self._split(0.72)  # the log has the floor
        self.paused = False
        self.button.set_label("PAUSE")
        self.stop_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.status.config(text="on the scope…" if scope else "stacking…")
        self.set_face("focused")
        self._schedule_blink()
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()

    # scopepull's exit codes, and what the owl does about them. This is the
    # table starstack used to own (docs/STARSTACK.md); the window owns it now
    # because the window is what decides whether to stack on top of a pull.
    PULL_VERDICT = {
        0: (None, True),
        2: ("nothing new on the scope. Go to bed. (Stacking what's already there.)", True),
        3: ("scope unreachable -- are you on its Wi-Fi?", False),
        4: ("Direct Data Download is off -- turn it on in the Unistellar app, then retry.", False),
        5: ("some observations didn't finish; they'll retry next press. Stacking the rest.", True),
    }

    def _run(self, steps):
        """Each step in turn, stopping when one says there's nothing worth
        going on for. Reports the last exit code we saw."""
        rc = 0
        self._stopping = False
        for kind, cmd in steps:
            rc = self._spawn(cmd)
            if self._stopping:
                break
            if rc is None:
                rc = 1
                break
            if kind == "pull":
                line, carry_on = self.PULL_VERDICT.get(
                    rc,
                    (
                        f"the pull exited with code {rc} and no explanation the owl recognises. "
                        f"Not stacking on top of that.",
                        False,
                    ),
                )
                if line:
                    self.q.put(("line", line + "\n"))
                if not carry_on:
                    break
        self.q.put(("done", rc))

    def _spawn(self, cmd):
        """Run one child, relaying its output into the log. None if it wouldn't
        start; otherwise its exit code."""
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=flags,
                env=env,
            )
            join_job(self.job, self.proc)
        except Exception as e:
            self.q.put(("line", f"couldn't start {os.path.basename(str(cmd[0]))}: {e}\n"))
            return None
        self._relay(self.proc.stdout)
        return self.proc.wait()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "roots":
                    self._roots_found(payload)
                elif kind == "starstack":
                    self._starstack_found(payload)
                elif kind == "seestar":
                    self._seestar_probed(payload)
                elif kind == "index":
                    self._fill_archive(*payload)
                elif kind == "version_latest":
                    self._version_latest(payload)
                elif kind == "install_done":
                    self._install_done(payload)
                elif kind == "line":
                    text = payload
                    tag = None
                    low = text.lower()
                    if (
                        "you're welcome" in low
                        or "they know what they did" in low
                        or "go outside" in low
                        or "go to bed" in low
                    ):
                        tag = "owl"
                    elif (
                        "already stacked this one" in low
                        or "nothing new on the scope" in low
                        or "nothing new to pull" in low
                    ):
                        tag = "owl"
                    elif (
                        "wouldn't" in low
                        or "failed" in low
                        or "couldn't" in low
                        or "error" in low
                        or "unreachable" in low
                        or "not stacking" in low
                        or "can't find" in low
                    ):
                        tag = "bad"
                    elif (
                        "ingested" in low
                        or text.startswith("  used")
                        or text.startswith("  output")
                    ):
                        tag = "good"
                    self.say(text, tag)
                    self._react(low)
                else:
                    self._finished(payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _glance(self, name, back, ms):
        """Pull a face for a moment, then return to `back` -- unless something
        else changed his mood in the meantime."""
        self.set_face(name)
        seq = self._face_seq

        def revert():
            if self._face_seq == seq and self.running() and not self.paused:
                self.set_face(back)

        self.after(ms, revert)

    def _react(self, low):
        """Which grumpy, based on what he just said."""
        if "building" in low or "waiting for the scope" in low or "combining" in low:
            # the scope is assembling the export, or the stacker is grinding:
            # nothing to do but wait. eyes shut.
            self.status.config(text="on the scope…" if "building" in low else "stacking…")
            if self.face != "waiting":
                self.set_face("waiting")
        elif "downloading" in low or "copying" in low or "ingested" in low:
            self.status.config(text="pulling…")
            if self.face != "focused":
                self.set_face("focused")
        elif low.startswith("pulling ") or low.startswith("whole night:") or low.startswith("[1/"):
            self.set_face("focused")
        elif "nothing new" in low or "already stacked this one" in low or "already had" in low:
            self._glance("judging", "focused", 1500)
        elif "skipped (not a deep-sky stack)" in low:
            self._glance("judging", "focused", 1200)
        elif low.startswith("  registering") or "reference:" in low or "debayering" in low:
            if self.face != "focused":
                self.set_face("focused")
        elif "dropped" in low and "quality check" in low:
            self._glance("judging", "focused", 3000)
        elif "writing " in low:
            self.set_face("focused")

    def _finished(self, rc):
        self.paused = False
        if self.settings.get("restack"):  # a one-off; nobody wants 95 minutes of M81 nightly
            self.settings["restack"] = False
            save_settings(self.settings)
        self.button.set_label(self.button_label())
        self.button.set_enabled(True)
        self.stop_btn.config(state="disabled")
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None
        if rc in (0, 2, 5):
            self.status.config(text="done. go outside.")
            self.set_face("grudging")  # the most he will give you
            if self._stacked_this_run:
                self._split(0.4)  # the picture has the floor now
                self.after(120, self._show_preview)
            else:
                self._split(0.3)  # the archive has the floor now
        else:
            self.status.config(text=f"stopped (exit {rc}). read the log.")
            self.set_face("furious")
            if self._stacked_this_run:
                self.preview_lbl.config(image="", text="no picture this time")
        self.refresh_archive(select_new=(rc in (0, 5) and self._pulled_this_run))
        if self._pulled_this_run:
            threading.Thread(target=self._probe_seestar, daemon=True).start()

    def _on_resize(self, _):
        if self.preview_path is None and not self.out_dir:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, lambda: (not self.running()) and self._show_preview())

    def _show_preview(self):
        path = self.preview_path
        if not path and self.out_dir and os.path.isdir(self.out_dir):
            looks = sorted(p for p in os.listdir(self.out_dir) if p.endswith("_look.png"))
            path = os.path.join(self.out_dir, looks[-1]) if looks else None
        if not path or not os.path.exists(path):
            self.preview_lbl.config(image="", text="no preview written")
            return
        try:
            img = tk.PhotoImage(file=path)
            w = max(200, self.preview_frame.winfo_width() - 20)
            h = max(120, self.preview_frame.winfo_height() - 20)
            k = max(1, int(-(-max(img.width() / w, img.height() / h) // 1)))  # ceil
            if k > 1:
                img = img.subsample(k, k)
            self.preview_img = img
            self.preview_lbl.config(image=img, text="")
        except tk.TclError as e:
            self.preview_lbl.config(image="", text=f"couldn't show preview: {e}")

    # ---- the archive --------------------------------------------------------
    def _archive_root(self):
        """The archive the table shows: the ticked scope's, else the Folder box
        if it's a folder, else the Odyssey's."""
        scope = self.pulling()
        f = self._real(self.folder_entry, self.folder)
        if scope:
            return f or self.roots[scope]
        if f and os.path.isdir(f):
            return f
        return self.roots["odyssey"]

    def _folder_reindex(self):
        if getattr(self, "_hinting", False) or not hasattr(self, "tree"):
            return
        if getattr(self, "_reindex_job", None):
            self.after_cancel(self._reindex_job)
        self._reindex_job = self.after(700, self.refresh_archive)

    def refresh_archive(self, select_new=False):
        if not hasattr(self, "tree"):
            return
        root = self._archive_root()
        self.archive_root = root
        scope = self.pulling() or "odyssey"
        args = ["index", "--scope", scope, "--root", root]

        def work():
            import json

            rows = []
            r = _run_cli(args, timeout=300)
            if r is not None:
                for line in (r.stdout or "").splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass
            self.q.put(("index", (root, rows, select_new)))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _pretty(sec):
        m = int(sec // 60)
        return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m" if m else f"{int(sec)}s"

    def _fill_archive(self, root, rows, select_new):
        if root != self.archive_root:
            return  # the scope changed while we were counting
        import time as _time

        self.archive_rows = rows
        self.tree.delete(*self.tree.get_children())
        by_target = {}
        for r in rows:
            by_target.setdefault(r["target"], []).append(r)
        now = _time.time()
        newest = None

        def ago(t):
            d = now - t
            return (
                "just now"
                if d < 3600
                else f"{int(d // 3600)}h ago"
                if d < 86400
                else "yesterday"
                if d < 2 * 86400
                else f"{int(d // 86400)}d ago"
            )

        for target in sorted(
            by_target, key=lambda t: max(r["night"] for r in by_target[t]), reverse=True
        ):
            rs = sorted(by_target[target], key=lambda r: r["night"], reverse=True)
            total = sum(r["integration_s"] for r in rs)
            frames = sum(r["frames"] for r in rs)
            tid = self.tree.insert(
                "",
                "end",
                text=target,
                open=True,
                values=(
                    f"{len(rs)} night{'s' if len(rs) != 1 else ''}",
                    frames,
                    self._pretty(total),
                    "",
                ),
                tags=("target",),
            )
            for r in rs:
                fresh = now - r["pulled_at"] < 6 * 3600
                iid = self.tree.insert(
                    tid,
                    "end",
                    text="",
                    values=(
                        r["night"] + ("  (mosaic)" if r.get("mosaic") else ""),
                        r["frames"],
                        self._pretty(r["integration_s"]),
                        ago(r["pulled_at"]),
                    ),
                    tags=("fresh",) if fresh else (),
                )
                if newest is None or r["pulled_at"] > newest[0]:
                    newest = (r["pulled_at"], iid)
        if not rows:
            self.tree.insert("", "end", text=f"nothing in {root} yet", tags=("empty",))
        n_nights = len({r["night"] for r in rows})
        total = sum(r["integration_s"] for r in rows)
        self.status.config(
            text=f"archive: {len(rows)} session{'s' if len(rows) != 1 else ''}, "
            f"{n_nights} night{'s' if n_nights != 1 else ''}, {self._pretty(total)} of sky"
            if rows
            else "archive: empty. Pull something."
        )
        if select_new and newest:
            self.tree.selection_set(newest[1])
            self.tree.see(newest[1])
        self._selection_changed()

    def _selected(self):
        """(kind, folder-or-target, rows): "night" = one session folder,
        "target" = all its nights."""
        sel = self.tree.selection()
        if not sel:
            return None, None, []
        iid = sel[0]
        parent = self.tree.parent(iid)
        if parent:
            night = self.tree.set(iid, "night").split("  ")[0]
            target = self.tree.item(parent, "text")
            rows = [r for r in self.archive_rows if r["target"] == target and r["night"] == night]
            return ("night", rows[0]["folder"], rows) if rows else (None, None, [])
        target = self.tree.item(iid, "text")
        rows = [r for r in self.archive_rows if r["target"] == target]
        return ("target", target, rows) if rows else (None, None, [])

    def _selection_changed(self):
        kind, what, rows = self._selected()
        on = "normal" if kind and not self.running() else "disabled"
        self.open_btn.config(state=on)
        self.sw_btn.config(state=on)
        if kind == "target" and len(rows) > 1:
            self.sw_btn.config(text=f"Combine {len(rows)} nights → StackingWizard")
        else:
            self.sw_btn.config(text="Open in StackingWizard")

    def _handoff_folder(self):
        """The folder to hand to a stacker: the night, or every night of the
        target gathered into combined/<target> (hard links)."""
        kind, what, rows = self._selected()
        if kind == "night":
            return what
        if kind != "target":
            return None
        if len(rows) == 1:
            return rows[0]["folder"]
        scope = self.pulling() or "odyssey"
        r = _run_cli(["combine", what, "--scope", scope, "--root", self.archive_root], timeout=900)
        if r is None:
            self.say("couldn't combine: the command wouldn't run.\n", "bad")
            return None
        dest = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("combined: "):
                dest = line[len("combined: ") :].strip()
            elif line.startswith("combined "):
                self.say(line + "\n", "owl")
            elif line.startswith("  "):
                self.say(line + "\n")
        if dest is None:
            self.say(((r.stdout or "") + (r.stderr or "")).strip() + "\n", "bad")
        return dest

    def open_folder(self):
        d = self._handoff_folder()
        if d and os.path.isdir(d):
            self._open_path(d)

    def _open_path(self, d):
        if sys.platform.startswith("win"):
            os.startfile(d)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def open_stackingwizard(self):
        """Hand the selected night (or combined target) to StackingWizard. It
        takes a folder by picker or drag-and-drop; the owl launches it with the
        folder as an argument in case it ever listens, puts the path on the
        clipboard for its picker, and opens the folder so it can be dragged."""
        d = self._handoff_folder()
        if not d or not os.path.isdir(d):
            return
        exe = self.sw_exe or find_stackingwizard(self.settings.get("stackingwizard", ""))
        if not exe:
            exe = filedialog.askopenfilename(title="Where is StackingWizard?", parent=self)
            if not exe:
                self.say(
                    f"StackingWizard not found. It's a free download: {STACKINGWIZARD_URL} -- put "
                    f"it in Downloads or on the Desktop and the owl will find it.\n",
                    "owl",
                )
                return
            self.settings["stackingwizard"] = exe
            save_settings(self.settings)
        self.sw_exe = exe
        try:
            self.clipboard_clear()
            self.clipboard_append(d)
        except tk.TclError:
            pass
        try:
            subprocess.Popen([exe, d], cwd=os.path.dirname(exe) or None)
        except OSError as e:
            self.say(f"couldn't start {exe}: {e}\n", "bad")
            return
        self._open_path(d)
        self.say(
            f"handed to StackingWizard: {d}\n  (the path is on your clipboard for its folder "
            f"picker, and the folder is open to drag across)\n",
            "owl",
        )
        self.status.config(text="over to StackingWizard.")

    def toggle_pause(self):
        if not (self.proc and self.proc.poll() is None):
            return
        if not self.paused:
            if not pause_tree(self.proc):
                self.say(
                    "\ncan't pause on this system (pip install psutil). Stop is over there.\n",
                    "bad",
                )
                return
            self.paused = True
            self.button.set_label("RESUME")
            self.status.config(text="paused.")
            if self._blink_job:
                self.after_cancel(self._blink_job)
                self._blink_job = None
            self._face_before_pause = self.face
            self.set_face("waiting")
            self.say(
                "\npaused. fine. the photons will wait. (the time-left estimate won't know "
                "about this.)\n",
                "owl",
            )
        else:
            resume_tree(self.proc)
            self.paused = False
            self.button.set_label("PAUSE")
            self.status.config(text="working…")
            self.set_face(getattr(self, "_face_before_pause", "focused"))
            self._schedule_blink()
            self.say("resuming.\n", "owl")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            # Checked by _run between steps. Without it, "Stop" during a pull
            # only skips the quick look by accident -- because a killed child's
            # exit code happens to fall outside PULL_VERDICT.
            self._stopping = True
            kill_tree(self.proc)
            self.paused = False
            self.say("\nstopped. fine.\n", "bad")

    def quit_app(self):
        """Closing the window ends the run and everything it started. In
        starstack 0.2.13 this was the bug: closing left the pull running unseen,
        the orphan kept its .zip.partial open, and every later pull of that
        observation died at the rename with "being used by another process".
        The job object (make_job) covers the case this handler can't: the exe
        killed from Task Manager."""
        if self.running():
            try:
                kill_tree(self.proc)
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
