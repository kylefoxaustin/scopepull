"""Configuration and per-OS paths.

All filesystem locations come from platformdirs so Linux and Windows behave
identically; nothing is hardcoded to ~/.config.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "scopepull"

DEFAULT_SCOPE_IP = "192.168.100.1"


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME))


def manifest_db_path() -> Path:
    return data_dir() / "manifest.db"


def default_archive_root() -> Path:
    # ~/Astro/odyssey on Linux, %USERPROFILE%\Astro\odyssey on Windows.
    return Path.home() / "Astro" / "odyssey"


@dataclass
class Config:
    scope_ip: str = DEFAULT_SCOPE_IP
    archive_root: Path = field(default_factory=default_archive_root)
    # FITS export is broken on Odyssey fw 4.2 and PNG is flaky; TIFF is lossless
    # and reliable, converted to FITS locally during ingest. See docs/API.md.
    format: str = "tiff"

    @property
    def base_url(self) -> str:
        return f"http://{self.scope_ip}"

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config.toml if present; otherwise defaults. Unknown keys ignored."""
        path = path or config_dir() / "config.toml"
        if not path.is_file():
            return cls()
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg = cls()
        scope_ip = raw.get("scope_ip")
        if isinstance(scope_ip, str):
            cfg.scope_ip = scope_ip
        archive_root = raw.get("archive_root")
        if isinstance(archive_root, str):
            cfg.archive_root = Path(archive_root).expanduser()
        fmt = raw.get("format")
        if fmt in ("fits", "tiff", "png"):
            cfg.format = fmt
        return cfg
