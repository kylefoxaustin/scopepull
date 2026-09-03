"""ScopeClient + EventPump against the mock scope (in-process ASGI transport)."""

from __future__ import annotations

import httpx
import pytest

from scopepull.client import DDDNotEnabled, ScopeClient


async def test_health(client):
    assert await client.health() is True


async def test_list_observations(client):
    obs = await client.list_observations()
    # mock has 2 entries (M101, Jupiter); NaN in Jupiter's tag_sc parses fine
    assert len(obs) == 2
    assert obs[0].target == "M101 - Pinwheel Galaxy"
    assert obs[1].target == "Jupiter"


async def test_ddd_disabled_detected(mock_app, scope_state):
    scope_state["ddd_enabled"] = False
    transport = httpx.ASGITransport(app=mock_app)
    async with ScopeClient("http://192.168.100.1", transport=transport) as c:
        with pytest.raises(DDDNotEnabled):
            await c.list_observations()
        assert await c.ddd_enabled() is False


async def test_ddd_enabled(client):
    assert await client.ddd_enabled() is True


async def test_zip_gate_502_without_pump(client, scope_state):
    """The mock reproduces evsoft's gate: no event poll -> 502."""
    resp = await client._http.get("/api/observations/zip/tiff/0x0/prod/obs-0001")
    assert resp.status_code == 502


async def test_zip_streams_with_pump(client, scope_state):
    async with client.event_pump() as pump:
        await pump.wait_ready(timeout=5)
        resp = await client._http.get("/api/observations/zip/tiff/0x0/prod/obs-0001")
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"


async def test_pump_ready_means_second_cycle(client):
    async with client.event_pump() as pump:
        await pump.wait_ready(timeout=5)
        assert pump.alive
    assert not pump.alive  # cancelled on exit


async def test_cancel_download_resets_build(client, scope_state):
    scope_state["build"] = "building"
    scope_state["progress"] = 2
    await client.cancel_download()
    assert scope_state["build"] == "idle"
    assert scope_state["progress"] == 0
    assert scope_state["cancel_count"] == 1


async def test_cancel_download_swallows_transport_errors():
    """Cancel is used on abort paths where the link may be gone — never raises."""

    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("gone")

    async with ScopeClient("http://192.168.100.1", transport=DeadTransport()) as c:
        await c.cancel_download()  # must not raise


async def test_download_progress_events(client, scope_state):
    """The pump captures 'started' progress from an in-flight build."""
    import asyncio

    scope_state["build_frames"] = 5
    async with client.event_pump() as pump:
        await pump.wait_ready(timeout=5)
        # Trigger a build via a zip GET (returns the empty zip, starts building).
        await client._http.get("/api/observations/zip/tiff/0x0/prod/obs-0001")
        for _ in range(200):
            if pump.progress.frames_total == 5 and pump.progress.frames_done > 0:
                break
            await asyncio.sleep(0.02)
    assert pump.progress.frames_total == 5
    assert pump.progress.frames_done > 0
    assert pump.progress.status in ("started", "ended")
