"""The Scope protocol and the Odyssey adapter: dispatch, exit codes, progress."""

from __future__ import annotations

import pytest

from scopepull.config import Config
from scopepull.log import Log
from scopepull.scopes import (
    EXIT_DDD_DISABLED,
    EXIT_NOTHING_NEW,
    EXIT_UNREACHABLE,
    Found,
    PullResult,
    get_scope,
    scope_name,
)
from scopepull.scopes.odyssey import OdysseyScope, build_status
from scopepull.transfer import ProgressEvent


def test_scope_name_defaults_to_odyssey():
    assert scope_name(None) == "odyssey"
    assert scope_name("Odyssey") == "odyssey"
    with pytest.raises(KeyError):
        scope_name("celestron")


def test_get_scope_gives_an_object_that_keeps_the_protocol(tmp_path):
    sc = get_scope("odyssey", Config(archive_root=tmp_path))
    assert (sc.name, sc.archive_root()) == ("odyssey", tmp_path)
    assert callable(sc.find) and callable(sc.pull)


def test_found_is_falsey_when_the_scope_isnt_there():
    assert not Found(None, "no such host", EXIT_UNREACHABLE)
    assert Found(["obs"], "at 192.168.100.1")
    assert PullResult().code == 0


def test_find_maps_scope_errors_to_the_contract_exit_codes(tmp_path, monkeypatch):
    from scopepull.client import DDDNotEnabled, ScopeUnreachable

    sc = OdysseyScope(Config(archive_root=tmp_path))

    async def unreachable(self, **kw):
        raise ScopeUnreachable("no route to 192.168.100.1")

    async def ddd_off(self, **kw):
        raise DDDNotEnabled("Direct Data Download is off")

    monkeypatch.setattr("scopepull.client.ScopeClient.list_observations", unreachable)
    found = sc.find(None, Log(lambda _t: None))
    assert not found and found.code == EXIT_UNREACHABLE and "no route" in found.how

    monkeypatch.setattr("scopepull.client.ScopeClient.list_observations", ddd_off)
    found = sc.find(None, Log(lambda _t: None))
    assert not found and found.code == EXIT_DDD_DISABLED


def test_find_passes_at_through_as_the_scope_ip(tmp_path, monkeypatch):
    cfg = Config(archive_root=tmp_path)
    sc = OdysseyScope(cfg)

    async def nothing(self, **kw):
        return []

    monkeypatch.setattr("scopepull.client.ScopeClient.list_observations", nothing)
    found = sc.find("10.0.0.9", Log(lambda _t: None))
    assert cfg.scope_ip == "10.0.0.9" and found and "10.0.0.9" in found.how


def test_pull_with_nothing_to_do_is_exit_2(tmp_path):
    lines: list[str] = []
    sc = OdysseyScope(Config(archive_root=tmp_path), manifest_db=tmp_path / "m.db")
    res = sc.pull([], tmp_path, Log(lines.append))
    assert res.code == EXIT_NOTHING_NEW and res.copied == 0
    assert lines == ["Nothing new to pull."]


def test_pull_skips_what_the_manifest_already_has(tmp_path):
    from scopepull.catalog import Observation
    from scopepull.manifest import Manifest
    from tests.mock_scope import OBSERVATIONS

    obs = Observation.from_raw(OBSERVATIONS[0])
    with Manifest(tmp_path / "m.db") as m:
        m.record(
            obs_id=obs.obs_id,
            target=obs.target,
            started_at=obs.started_at,
            frame_count=2,
            format="tiff",
            dir="x",
            files=[],
        )
    sc = OdysseyScope(Config(archive_root=tmp_path), manifest_db=tmp_path / "m.db")
    assert sc.pull([obs], tmp_path, Log(lambda _t: None)).code == EXIT_NOTHING_NEW


def test_build_status_reads_as_a_sentence():
    building = ProgressEvent(
        obs_id="x", phase="downloading", frames_done=187, frames_total=364, elapsed_s=200.0
    )
    text = build_status(building)
    assert text.startswith("building 187/364 frames · 3m20s") and "left" in text

    streaming = ProgressEvent(
        obs_id="x", phase="downloading", bytes_done=220_200_960, elapsed_s=400.0
    )
    assert build_status(streaming).startswith("downloading 210MB · ")
