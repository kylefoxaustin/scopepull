"""ScopeClient + EventPump against the mock scope (in-process ASGI transport)."""

from __future__ import annotations

import httpx
import pytest

from scopepull.client import DDDNotEnabled, ScopeClient, ScopeUnreachable


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


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_listing_http_error_is_scope_unreachable(mock_app, scope_state, status):
    """A non-2xx from the list endpoint must not escape as httpx.HTTPStatusError
    (exit 1, raw traceback); it is ScopeUnreachable, so `pull` exits 3 with a
    sentence. Found when `starstack --pull` ran with a proxy answering 403."""
    scope_state["list_status"] = status
    transport = httpx.ASGITransport(app=mock_app)
    async with ScopeClient("http://192.168.100.1", transport=transport) as c:
        with pytest.raises(ScopeUnreachable, match=f"HTTP {status}"):
            await c.list_observations()


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


async def test_cancel_download_marks_suppressed(client, scope_state):
    scope_state["building"] = True
    scope_state["progress"] = 2
    await client.cancel_download()
    assert scope_state["building"] is False
    assert scope_state["progress"] == 0
    assert scope_state["suppressed"] is True
    assert scope_state["cancel_count"] == 1


async def test_cancel_download_swallows_transport_errors():
    """Cancel is used on abort paths where the link may be gone — never raises."""

    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("gone")

    async with ScopeClient("http://192.168.100.1", transport=DeadTransport()) as c:
        await c.cancel_download()  # must not raise


async def test_download_progress_events(client, scope_state):
    """The pump captures 'started' progress emitted during a held zip GET."""
    import asyncio

    scope_state["build_frames"] = 5
    scope_state["build_seconds"] = 0.6
    async with client.event_pump() as pump:
        await asyncio.sleep(0.1)  # let the pump start polling
        # Kick a held GET in the background (it drives the build events).
        task = asyncio.create_task(client._http.get("/api/observations/zip/tiff/0x0/prod/obs-0001"))
        for _ in range(200):
            if pump.progress.frames_total == 5 and pump.progress.frames_done > 0:
                break
            await asyncio.sleep(0.02)
        await task
    assert pump.progress.frames_total == 5
    assert pump.progress.frames_done > 0


def test_keepalive_socket_options_present():
    """Real ScopeClient enables TCP keepalive; SO_KEEPALIVE is always included."""
    import socket

    from scopepull.client import _keepalive_socket_options

    opts = _keepalive_socket_options()
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in opts
    # Every entry is a (level, optname, value) int triple setsockopt accepts.
    assert all(len(o) == 3 and all(isinstance(x, int) for x in o) for o in opts)
    # On Linux the interval knobs must be present (they carry the real behavior).
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert any(o[1] == socket.TCP_KEEPIDLE for o in opts)


def test_real_client_builds_with_keepalive_transport():
    """Constructing ScopeClient without an injected transport works (keepalive path)."""
    from scopepull.client import ScopeClient

    c = ScopeClient("http://192.168.100.1")
    assert c._http is not None
