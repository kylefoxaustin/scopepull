"""transfer.pull against the mock's build-then-download state machine."""

from __future__ import annotations

import httpx
import pytest

from scopepull.catalog import Observation
from scopepull.client import ScopeClient
from scopepull.transfer import TransferError, pull, zip_has_frames
from tests.mock_scope import EMPTY_ZIP, OBSERVATIONS, make_calibration_zip


def _obs() -> Observation:
    return Observation.from_raw(dict(OBSERVATIONS[0]))


def _client(app):
    return ScopeClient("http://192.168.100.1", settle=0.0, transport=httpx.ASGITransport(app=app))


async def _run(client, obs, dest, **kw):
    events = []
    async for ev in pull(client, obs, dest, build_timeout=kw.get("build_timeout", 20)):
        events.append(ev)
    return events


async def test_pull_build_then_download(mock_app, scope_state, tmp_path):
    """Full protocol: trigger -> building (started) -> ended -> download."""
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    phases = [e.phase for e in events]
    assert "trigger" in phases
    assert "building" in phases
    assert "downloading" in phases
    assert events[-1].phase == "done"
    # We must NOT have cancelled during the build — exactly one clear at start.
    assert scope_state["cancel_count"] == 1


async def test_pull_waits_for_ended_not_just_first_empty(mock_app, scope_state, tmp_path):
    # Make the build take several polls; the pull must keep waiting, not give up.
    scope_state["build_frames"] = 8
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest, build_timeout=30)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    assert events[-1].phase == "done"


async def test_pull_times_out_if_build_never_ends(mock_app, scope_state, tmp_path):
    scope_state["build_frames"] = 10_000  # will never finish in the budget
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest, build_timeout=1)
    await client.aclose()
    assert not dest.exists()
    assert not dest.with_suffix(".zip.partial").exists()


async def test_pull_rejects_manifest_only(mock_app, scope_state, tmp_path):
    scope_state["manifest_only"] = True
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest, build_timeout=20)
    await client.aclose()
    assert not dest.exists()


async def test_pull_fails_on_dropped_download(mock_app, scope_state, tmp_path):
    # The post-"ended" download drops mid-stream (short read).
    scope_state["flaky_after"] = 5000
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest, build_timeout=20)
    await client.aclose()
    assert not dest.exists()


def test_zip_has_frames_rejects_empty(tmp_path):
    p = tmp_path / "e.zip"
    p.write_bytes(EMPTY_ZIP)
    assert not zip_has_frames(p)


def test_zip_has_frames_accepts_real(tmp_path):
    p = tmp_path / "r.zip"
    p.write_bytes(make_calibration_zip(OBSERVATIONS[0]))
    assert zip_has_frames(p)
