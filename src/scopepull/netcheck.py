"""Am I on the scope's Wi-Fi? Friendly connectivity diagnostics."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from .platform import current_ssids

SCOPE_SSID_PREFIXES = ("Odyssey-", "UNI-", "eVscope-")


@dataclass(frozen=True)
class NetStatus:
    reachable: bool
    ssids: tuple[str, ...]  # SSIDs of currently-connected Wi-Fi interfaces
    on_scope_ssid: bool  # any connected SSID looks like a scope AP

    def diagnosis(self, scope_ip: str) -> str:
        """One human sentence about what's wrong (empty if nothing is)."""
        if self.reachable:
            return ""
        if self.on_scope_ssid:
            return (
                f"On a scope-looking Wi-Fi but {scope_ip} doesn't answer — "
                "is the scope still powering up, or did the AP drop?"
            )
        if self.ssids:
            joined = ", ".join(self.ssids)
            return (
                f"Connected to {joined!r}, not a scope network — join the "
                "Odyssey-xxxx / UNI-xxxx / eVscope-xxxx Wi-Fi and retry."
            )
        return (
            "No Wi-Fi connection detected — join the scope's Odyssey-xxxx / "
            "UNI-xxxx / eVscope-xxxx network."
        )


async def tcp_reachable(host: str, port: int = 80, timeout: float = 3.0) -> bool:
    """Fast reachability probe: can we open TCP to the scope's web server?"""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def check(scope_ip: str) -> NetStatus:
    reachable = await tcp_reachable(scope_ip)
    ssids = tuple(current_ssids())
    on_scope = any(s.startswith(SCOPE_SSID_PREFIXES) for s in ssids)
    return NetStatus(reachable=reachable, ssids=ssids, on_scope_ssid=on_scope)
