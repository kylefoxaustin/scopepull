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


def default_archive_root(scope: str = "odyssey") -> Path:
    # ~/Astro/<scope> on Linux, %USERPROFILE%\Astro\<scope> on Windows.
    # One root per scope: the two archives have different layouts inside a
    # night, and keeping them apart means `index` and `combine` never have to
    # guess which kind of session they are looking at.
    return Path.home() / "Astro" / scope


@dataclass
class Config:
    scope_ip: str = DEFAULT_SCOPE_IP
    # The Odyssey's root. Still a field of its own because `status` prints it as
    # "Archive root:" and starstack parses that line (docs/STARSTACK.md).
    archive_root: Path = field(default_factory=default_archive_root)
    # Per-scope overrides, from [archive_roots] in config.toml.
    archive_roots: dict[str, Path] = field(default_factory=dict)
    # FITS export is broken on Odyssey fw 4.2 and PNG is flaky; TIFF is lossless
    # and reliable, converted to FITS locally during ingest. See docs/API.md.
    format: str = "tiff"

    def root_for(self, scope: str) -> Path:
        """Where this scope keeps its nights."""
        if scope in self.archive_roots:
            return self.archive_roots[scope]
        if scope == "odyssey":
            return self.archive_root
        return default_archive_root(scope)

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
        roots = raw.get("archive_roots")
        if isinstance(roots, dict):
            for name, value in roots.items():
                if isinstance(value, str):
                    cfg.archive_roots[str(name)] = Path(value).expanduser()
            # [archive_roots] odyssey = ... and the legacy top-level
            # archive_root are the same setting; the explicit one wins.
            if "odyssey" in cfg.archive_roots:
                cfg.archive_root = cfg.archive_roots["odyssey"]
        fmt = raw.get("format")
        if fmt in ("fits", "tiff", "png"):
            cfg.format = fmt
        return cfg
