"""The fixtures must never carry the scope owner's site, serial or hardware ID.

These repos are public. The scope stamps the observing site's GPS into every
observation to eleven decimal places — that is a home address — along with its
serial number, which is also its Wi-Fi SSID (`odyssey-<serial>`), and its MAC,
which rides in the node field of every UUIDv1 observation id and is therefore
guessably its AP's BSSID. Public BSSID-geolocation databases answer unauthenticated
lookups, so the MAC is an indirect route back to the thing the GPS scrub removes.

Until now the convention was enforced by whoever remembered it. This test is the
enforcement: it fails the build, on every machine, before a capture reaches a
commit. If you are adding a fixture and this fails, scrub it — don't widen the test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# A UUIDv1's last group is the device MAC. The real one is not written down
# anywhere in this repo; anything that isn't the scrubbed placeholder fails.
UUID_NODE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-([0-9a-f]{12})")
ALLOWED_NODES = {"000000000000"}
ALLOWED_SERIALS = {"SCRUBBED", "", "0"}
SITE_KEYS = ("lat", "long", "alt")


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.suffix.lower() in {".json", ".txt", ".log"})


def test_there_are_fixtures_to_check():
    assert _fixture_files(), "no fixtures found — has the layout moved?"


def test_no_site_coordinates_in_any_fixture():
    """lat/long/alt must be zero. A non-zero altitude alone would narrow a city
    to a neighbourhood."""
    bad = []
    for p in _fixture_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for key in SITE_KEYS:
            for value in re.findall(rf'"{key}"\s*:\s*(-?[0-9.eE+]+)', text):
                if float(value) != 0.0:
                    bad.append(f"{p.name}: {key} = {value}")
    assert not bad, "unscrubbed observing site in fixtures:\n  " + "\n  ".join(bad)


def test_no_scope_serial_in_any_fixture():
    """The serial is also the scope's Wi-Fi SSID."""
    bad = []
    for p in _fixture_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r'"sn"\s*:\s*"([^"]*)"', text):
            if value not in ALLOWED_SERIALS:
                bad.append(f"{p.name}: sn = {value!r}")
    assert not bad, "unscrubbed scope serial in fixtures:\n  " + "\n  ".join(bad)


def test_no_device_mac_in_any_fixture():
    """The UUIDv1 node field is the scope's MAC address."""
    bad = []
    for p in _fixture_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for node in set(UUID_NODE.findall(text)):
            if node not in ALLOWED_NODES:
                bad.append(f"{p.name}: uuid node = {node}")
    assert not bad, "unscrubbed device MAC in fixtures:\n  " + "\n  ".join(bad)


def test_the_scrubbed_fixtures_are_still_valid_json():
    """Scrubbing must not have broken what these are for."""
    for p in FIXTURES.rglob("*.json"):
        raw = p.read_text(encoding="utf-8")
        # the raw listing keeps the scope's bare NaN tokens on purpose
        json.loads(re.sub(r"\bNaN\b", "null", raw))
