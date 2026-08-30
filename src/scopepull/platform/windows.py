"""Windows platform helpers (netsh). Phase 4 adds Task Scheduler install."""

from __future__ import annotations

import subprocess


def current_ssids() -> list[str]:
    """SSIDs of currently-connected WLAN interfaces, via netsh. [] on any failure."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return []
    return parse_netsh_interfaces(out)


def parse_netsh_interfaces(out: str) -> list[str]:
    """Parse `netsh wlan show interfaces` output. Separated for testing.

    Looks for `SSID : <name>` lines on interfaces whose State is connected.
    netsh output is localized; we match on line shape (key : value) and accept
    the SSID key specifically, which is not translated.
    """
    ssids: list[str] = []
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        # Exact "SSID" only — excludes "BSSID" and "AP BSSID" lines.
        if key == "SSID":
            value = value.strip()
            if value:
                ssids.append(value)
    return ssids
