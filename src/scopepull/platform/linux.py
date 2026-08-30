"""Linux platform helpers (nmcli). Phase 4 adds systemd timer install."""

from __future__ import annotations

import shutil
import subprocess


def current_ssids() -> list[str]:
    """SSIDs of currently-active Wi-Fi connections, via nmcli. [] on any failure."""
    if not shutil.which("nmcli"):
        return []
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return parse_nmcli_wifi(out)


def parse_nmcli_wifi(out: str) -> list[str]:
    """Parse `nmcli -t -f ACTIVE,SSID dev wifi` output. Separated for testing."""
    ssids = []
    for line in out.splitlines():
        # terse format: yes:MySSID — SSID may contain escaped colons (\:)
        active, sep, ssid = line.partition(":")
        if sep and active == "yes":
            ssid = ssid.replace("\\:", ":").strip()
            if ssid:
                ssids.append(ssid)
    return ssids
