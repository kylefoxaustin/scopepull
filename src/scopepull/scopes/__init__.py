"""One app, several scopes: what a scope has to be able to do.

scopepull started as the Odyssey Pro's puller and is now *the* puller, so the
part that knows about a particular telescope lives behind four questions:

    where does this scope's archive live    archive_root()
    where is the scope right now            find(spec, log)   -> Found
    give me everything I haven't got        pull(handle, ...) -> PullResult
    did that work                           PullResult.code

Nothing else in scopepull knows which kind of telescope it is talking to, and a
new scope (the Dwarf, December) is one module implementing those.

The exit codes are the contract every scope keeps, not just the Odyssey's:
starstack and the window both read them to decide whether the archive is in a
state worth stacking (docs/STARSTACK.md spells out what each one makes
starstack do). So a Seestar that can't be found exits 3, the same as an Odyssey
that doesn't answer, and 0/2/5 keep meaning "the archive is worth stacking".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import Config
from ..log import Log

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING_NEW = 2
EXIT_UNREACHABLE = 3
EXIT_DDD_DISABLED = 4
EXIT_PARTIAL = 5

# Kept here rather than on the classes so `status`, `--help` and the window can
# name a scope without importing its module (and with it httpx or astropy).
LABELS: dict[str, str] = {
    "odyssey": "Unistellar Odyssey Pro",
    "seestar": "ZWO Seestar S50 / S50 Pro",
}
SCOPE_NAMES: tuple[str, ...] = ("odyssey", "seestar")
DEFAULT_SCOPE = "odyssey"


@dataclass
class Found:
    """The answer to "where is the scope?".

    `handle` is whatever that scope's `pull` needs (the Odyssey's listing, the
    Seestar's MyWorks folder) and is None when the scope isn't there; `how` is
    one human sentence either way -- "on Wi-Fi at seestar (192.168.1.50)" or the
    reason there's nothing to pull from. `code` carries which kind of
    not-there it was, because the exit code is part of the contract.
    """

    handle: Any | None
    how: str
    code: int = EXIT_OK

    def __bool__(self) -> bool:
        return self.handle is not None


@dataclass
class PullResult:
    """What a pull did.

    `copied`/`skipped` are in whatever unit the scope counts: observations for
    the Odyssey (one observation is a folder of frames plus its dark), single
    sub-frames for the Seestar. `nights` are the archive nights touched;
    `failed` names what didn't make it, and `code` is what the process exits
    with.
    """

    copied: int = 0
    skipped: int = 0
    nights: set[str] = field(default_factory=set)
    failed: list[str] = field(default_factory=list)
    code: int = EXIT_OK


class Scope(Protocol):
    """A telescope scopepull can pull from."""

    name: str
    label: str

    def archive_root(self) -> Path:
        """Where this scope's nights are kept."""
        ...

    def find(self, spec: str | None, log: Log) -> Found:
        """Locate the scope. `spec` is whatever the user passed to `--at`:
        an IP for the Odyssey, a drive/share/host for the Seestar, or None to
        go looking."""
        ...

    def pull(self, handle: Any, root: Path, log: Log, target: str | None = None) -> PullResult:
        """Copy everything `root` hasn't got yet. Read-only on the scope."""
        ...


def get_scope(name: str, cfg: Config, **opts: Any) -> Scope:
    """Build a scope by name.

    The imports are deliberately lazy: the Odyssey adapter pulls in httpx,
    numpy and astropy, which is a second of start-up that `scopepull index
    --scope seestar` and the window's archive table have no reason to pay.
    """
    if name == "odyssey":
        from .odyssey import OdysseyScope

        return OdysseyScope(cfg, **opts)
    if name == "seestar":
        from .seestar import SeestarScope

        # The Seestar is a folder: --new/--since/--format mean nothing to it
        # (it dedupes by name and size against the archive itself).
        return SeestarScope(cfg)
    raise KeyError(name)


def scope_name(name: str | None) -> str:
    """Validate a --scope value, with the Odyssey as the default it has always
    been (bare `scopepull` must keep meaning "pull the Odyssey")."""
    if not name:
        return DEFAULT_SCOPE
    low = name.strip().lower()
    if low not in SCOPE_NAMES:
        raise KeyError(name)
    return low
