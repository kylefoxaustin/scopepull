"""transfer.pull against the mock: warm-up retry, empty rejection, flaky drops."""

from __future__ import annotations

import httpx
import pytest

from scopepull.catalog import Observation
from scopepull.client import ScopeClient
from scopepull.transfer import TransferError, pull, zip_has_frames
from tests.mock_scope import EMPTY_ZIP, make_calibration_zip


def _obs() -> Observation:
    return Observation.from_raw(
        {
            "vpath": "prod/obs-0001",
            "name": "20260201T050025_166",
            "nameTarget": "M101 - Pinwheel Galaxy",
            "nb_frames": 6,
            "obs_start": 1787184000000,
            "type": "BAYER_GBRG",
        }
    )


async def _client(app):
    return ScopeClient("http://192.168.100.1", settle=0.0, transport=httpx.ASGITransport(app=app))


async def _run(client, obs, dest, **kw):
    events = []
    async for ev in pull(
        client, obs, dest, max_attempts=kw.get("max_attempts", 10), warmup=lambda a: 0.0
    ):  # no real sleeps in tests
        events.append(ev)
    return events


async def test_pull_succeeds_first_try(mock_app, scope_state, tmp_path):
    scope_state["warmup_empties"] = 0
    client = await _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    assert events[-1].phase == "done"


async def test_pull_retries_through_warmup_empties(mock_app, scope_state, tmp_path):
    # First 3 attempts return the instant empty zip; 4th returns real frames.
    scope_state["warmup_empties"] = 3
    client = await _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = await _run(client, _obs(), dest)
    await client.aclose()
    assert dest.exists() and zip_has_frames(dest)
    empties = [e for e in events if e.phase == "empty"]
    assert len(empties) == 3
    assert events[-1].phase == "done" and events[-1].attempt == 4


async def test_pull_gives_up_after_max_attempts(mock_app, scope_state, tmp_path):
    scope_state["warmup_empties"] = 99  # never succeeds
    client = await _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest, max_attempts=3)
    await client.aclose()
    assert not dest.exists()
    assert not dest.with_suffix(".zip.partial").exists()


async def test_pull_rejects_manifest_only(mock_app, scope_state, tmp_path):
    scope_state["manifest_only"] = True
    client = await _client(mock_app)
    dest = tmp_path / "obs.zip"
    with pytest.raises(TransferError):
        await _run(client, _obs(), dest, max_attempts=2)
    await client.aclose()
    assert not dest.exists()


async def test_pull_recovers_from_partial_transfer(mock_app, scope_state, tmp_path):
    # First job truncates mid-stream (short read); heal it so the retry completes.
    scope_state["flaky_after"] = 5000
    client = await _client(mock_app)
    dest = tmp_path / "obs.zip"
    events = []
    healed = False
    async for ev in pull(client, _obs(), dest, max_attempts=5, warmup=lambda a: 0.0):
        events.append(ev)
        # After attempt 1 fails (short body -> dropped or empty), heal the link.
        if not healed and ev.attempt == 1 and ev.phase in ("dropped", "empty"):
            scope_state["flaky_after"] = 0
            healed = True
    await client.aclose()
    # Attempt 1 must have failed, and a later attempt must have completed cleanly.
    assert any(e.attempt == 1 and e.phase in ("dropped", "empty") for e in events)
    assert events[-1].phase == "done" and events[-1].attempt >= 2
    assert dest.exists() and zip_has_frames(dest)


def test_zip_has_frames_rejects_empty(tmp_path):
    p = tmp_path / "e.zip"
    p.write_bytes(EMPTY_ZIP)
    assert not zip_has_frames(p)


def test_zip_has_frames_accepts_real(tmp_path):
    from tests.mock_scope import OBSERVATIONS

    p = tmp_path / "r.zip"
    p.write_bytes(make_calibration_zip(OBSERVATIONS[0]))
    assert zip_has_frames(p)
