"""transfer.pull against the mock's single-held-GET protocol."""

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
    async for ev in pull(client, obs, dest, read_timeout=kw.get("read_timeout", 20)):
        events.append(ev)
    return events


async def test_pull_single_get_downloads_and_ingests(mock_app, scope_state, tmp_path):
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    phases = [e.phase for e in events]
    assert phases[0] == "starting"
    assert "downloading" in phases
    assert events[-1].phase == "done"
    # No pre-cancel: the browser never cancels before the GET.
    assert scope_state["cancel_count"] == 0


async def test_pull_reports_build_progress(mock_app, scope_state, tmp_path):
    # Longer build window so the event pump surfaces frame progress.
    scope_state["build_frames"] = 6
    scope_state["build_seconds"] = 0.5
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    assert events[-1].phase == "done"


async def test_pull_rejects_manifest_only(mock_app, scope_state, tmp_path):
    scope_state["manifest_only"] = True
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest)
    await client.aclose()
    assert not dest.exists()


async def test_pull_fails_on_dropped_download(mock_app, scope_state, tmp_path):
    scope_state["flaky_after"] = 5000  # short read mid-stream
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest)
    await client.aclose()
    assert not dest.exists()
    assert not dest.with_suffix(".zip.partial").exists()


async def test_pull_rejects_empty_after_precancel(mock_app, scope_state, tmp_path):
    # Simulate a stale suppression (as if a cancel had preceded): GET returns empty.
    scope_state["suppressed"] = True
    client = _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest)
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
