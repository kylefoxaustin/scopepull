from datetime import UTC, datetime
from pathlib import Path

import pytest

from scopepull.catalog import Observation, parse_listing, slugify

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_listing_fixture_with_nan():
    body = (FIXTURES / "observations_list.json").read_text()
    obs = parse_listing(body)
    assert len(obs) == 2
    bubble, m31 = obs
    assert bubble.target == "NGC 7635"
    assert bubble.target_slug == "ngc-7635"
    assert bubble.frame_count == 1200
    assert bubble.started_at == datetime.fromtimestamp(1787184000, tz=UTC)
    # NaN frame count became null -> 0, not a crash
    assert m31.frame_count == 0


def test_parse_listing_wrapper_shapes():
    assert len(parse_listing('{"observations": [{"vpath": "a"}]}')) == 1
    assert len(parse_listing('{"vpath": "solo"}')) == 1
    assert parse_listing("{}") == []


def test_parse_listing_drops_vpathless_entries():
    assert parse_listing('[{"name": "no vpath"}]') == []


def test_parse_listing_rejects_garbage():
    with pytest.raises(ValueError):
        parse_listing("not json at all")


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("NGC 7635", "ngc-7635"),
        ("M 31", "m-31"),
        ("", "untargeted"),
        ("///", "untargeted"),
        ("CON", "untargeted"),  # Windows reserved device name
        ("Caldwell 11 (Bubble)", "caldwell-11-bubble"),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


def test_dir_name_stable():
    o = Observation.from_raw({"vpath": "obs/x/1", "name": "M 31", "obs_attr": {"tag_sc": "M 31"}})
    assert o.dir_name == f"m-31__{o.id_short}"
    assert len(o.id_short) == 8
