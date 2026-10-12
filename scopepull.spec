# PyInstaller spec for scopepull.exe -- one file, no console, owl icon.
#   python make_version_info.py && pyinstaller --noconfirm --clean scopepull.spec
#
# Lifted from starstack.spec when the owl moved in (docs/THE_OWL_MOVES_IN.md),
# with its two hard-won rules intact:
#   * the exe must NOT exclude astropy.tests (see the excludes below);
#   * the version resource must exist, or Defender treats the exe as something
#     thrown together to be thrown away (see make_version_info.py).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

here = os.path.abspath(".")
assets = os.path.join(here, "src", "scopepull", "assets")

datas = [
    (os.path.join(assets, "faces", "*.png"), os.path.join("scopepull", "assets", "faces")),
    (os.path.join(assets, "owl_256.png"), os.path.join("scopepull", "assets")),
    (os.path.join(assets, "icon_256.png"), os.path.join("scopepull", "assets")),
    (os.path.join(assets, "scopepull.ico"), os.path.join("scopepull", "assets")),
]
# The window looks for its faces next to itself (gui.ASSETS = HERE/assets, and
# HERE is sys._MEIPASS when frozen), so they go in twice: once where the
# package expects them, once where the window looks.
datas += [
    (os.path.join(assets, "faces", "*.png"), os.path.join("assets", "faces")),
    (os.path.join(assets, "owl_256.png"), "assets"),
    (os.path.join(assets, "icon_256.png"), "assets"),
    (os.path.join(assets, "scopepull.ico"), "assets"),
]
datas += collect_data_files("astropy", include_py_files=False)

hidden = (
    collect_submodules("scopepull")
    + collect_submodules("astropy.io.fits")
    + ["imagecodecs", "tifffile", "numpy", "httpx", "platformdirs", "typer", "rich"]
)

a = Analysis(
    ["scopepull_app.py"],
    pathex=[here, os.path.join(here, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # NOT astropy.tests: astropy/__init__.py imports astropy.tests.runner, so
    # excluding it makes `import astropy` fail inside the exe -- every FITS
    # frame "unreadable", TIFFs fine, nobody the wiser (starstack 0.2.5..0.2.13).
    excludes=["matplotlib", "IPython", "pytest", "tkinter.test", "scipy", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="scopepull",
    icon=os.path.join(assets, "scopepull.ico"),
    # Properties > Details: product name, version, author. make_version_info.py
    # writes this from __version__; without it the exe looks throwaway.
    version=(
        os.path.join(here, "version_info.txt")
        if os.path.exists(os.path.join(here, "version_info.txt"))
        else None
    ),
    console=False,  # no black console window behind the owl
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
