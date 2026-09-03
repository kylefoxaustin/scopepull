"""Observation catalog: parse the scope's /api/observations/list response.

Field knowledge is SOURCED from the prior-art repo (see docs/API.md); the raw
dict is always retained on the Observation so nothing the scope told us is
thrown away before we've seen real fixtures.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Windows-reserved device names; a target slug must never collide with these.
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def slugify(name: str) -> str:
    """Sanitize a target name into a cross-platform directory component.

    `NGC 7635` -> `ngc-7635`; empty/exotic input -> `untargeted`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or slug in _WINDOWS_RESERVED:
        return "untargeted"
    return slug


@dataclass(frozen=True)
class Observation:
    """One observation as listed by the scope."""

    vpath: str  # opaque download key — the true identity
    name: str
    target: str  # best-available target label
    purpose: str
    pmode: str
    started_at: datetime | None  # from obs_start (epoch ms, UTC)
    frame_count: int
    raw: dict[str, Any] = field(compare=False, hash=False, repr=False, default_factory=dict)

    @property
    def obs_id(self) -> str:
        """Stable identifier: the vpath (unique per observation on the scope)."""
        return self.vpath

    @property
    def id_short(self) -> str:
        """8-char digest of the vpath, for directory names."""
        return hashlib.sha256(self.vpath.encode()).hexdigest()[:8]

    @property
    def target_slug(self) -> str:
        return slugify(self.target)

    @property
    def date_str(self) -> str:
        """Local-timezone date of observation start, for the archive layout."""
        if self.started_at is None:
            return "unknown-date"
        return self.started_at.astimezone().strftime("%Y-%m-%d")

    @property
    def dir_name(self) -> str:
        return f"{self.target_slug}__{self.id_short}"

    @classmethod
    def from_raw(cls, obj: dict[str, Any]) -> Observation:
        obs_start = obj.get("obs_start")
        started_at = None
        if isinstance(obs_start, (int, float)) and obs_start > 0:
            try:
                started_at = datetime.fromtimestamp(obs_start / 1000, tz=UTC)
            except (OSError, OverflowError, ValueError):
                started_at = None

        obs_attr = obj.get("obs_attr") or {}
        # Odyssey fw 4.2 puts the display name in nameTarget; eVscope-era
        # firmware used obs_attr.tag_sc. Fall back through both, then name.
        tag = obs_attr.get("tag_sc") if isinstance(obs_attr, dict) else None
        name = str(obj.get("name") or "")
        nb = obj.get("nb_frames")
        return cls(
            vpath=str(obj.get("vpath") or ""),
            name=name,
            target=str(obj.get("nameTarget") or tag or name or "untargeted"),
            purpose=str(obj.get("purpose") or ""),
            pmode=str(obj.get("pmode") or ""),
            started_at=started_at,
            frame_count=int(nb) if isinstance(nb, (int, float)) else 0,
            raw=obj,
        )


def parse_listing(body: str) -> list[Observation]:
    """Parse the raw /api/observations/list body into Observations.

    Handles the scope's invalid-JSON quirk (bare NaN tokens) and the wrapper
    shapes prior art saw in the wild. Raises ValueError on unparseable input.
    """
    # The scope emits bare NaN for some numeric fields — invalid JSON.
    data = json.loads(re.sub(r"\bNaN\b", "null", body))

    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("observations", "obs", "data", "list", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [data] if "vpath" in data else []
    else:
        raise ValueError(f"unexpected listing type: {type(data).__name__}")

    out = [Observation.from_raw(o) for o in items if isinstance(o, dict)]
    return [o for o in out if o.vpath]
