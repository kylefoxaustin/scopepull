"""Parsers only — never shell out in CI (spec §8)."""

from scopepull.platform.linux import parse_nmcli_wifi
from scopepull.platform.windows import parse_netsh_interfaces

NMCLI_OUT = """\
yes:UNI-2E4F
no:NeighborNet
no:--
yes:Home\\:5G
"""

NETSH_OUT = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6E AX211
    GUID                   : deadbeef-0000-0000-0000-000000000000
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : UNI-2E4F
    BSSID                  : 11:22:33:44:55:66
    Network type           : Infrastructure
    Radio type             : 802.11n
    Authentication         : WPA2-Personal
"""


def test_parse_nmcli():
    assert parse_nmcli_wifi(NMCLI_OUT) == ["UNI-2E4F", "Home:5G"]


def test_parse_nmcli_empty():
    assert parse_nmcli_wifi("") == []


def test_parse_netsh():
    assert parse_netsh_interfaces(NETSH_OUT) == ["UNI-2E4F"]


def test_parse_netsh_excludes_bssid():
    out = "    BSSID : 11:22:33:44:55:66\n    AP BSSID : 11:22:33:44:55:66\n"
    assert parse_netsh_interfaces(out) == []
