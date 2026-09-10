"""ScopeClient: async HTTP client for the Odyssey's DDD API.

Protocol knowledge is documented in docs/API.md. The critical behavior: the
scope will not stream zip data unless a client is concurrently long-polling
GET /api/event — EventPump provides that as an async context manager.

Nothing here prints; callers observe progress via the pump's state and the
events the transfer layer yields.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from . import __version__
from .catalog import Observation, parse_listing

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15.0
LIST_TIMEOUT = 120.0  # observation list can take minutes on a full scope
EVENT_POLL_TIMEOUT = 30.0  # scope holds the long-poll ~30 s when idle
FIRST_POLL_TIMEOUT = 5.0  # short first poll to confirm the backend saw us
READ_TIMEOUT = 300.0  # per-chunk read timeout on zip streams

USER_AGENT = f"scopepull/{__version__}"

ALLOWED_FORMATS = ("fits", "tiff", "png")


class ScopeError(Exception):
    """Base for scope-side failures."""


class ScopeUnreachable(ScopeError):
    """Can't talk to the scope at all (wrong Wi-Fi, scope off)."""


class DDDNotEnabled(ScopeError):
    """Scope reachable but Direct Data Download appears to be off."""


class PumpDead(ScopeError):
    """The event pump died while a transfer depended on it."""


@dataclass
class DownloadProgress:
    """Server-side export progress reported over /api/event."""

    status: str = ""
    frames_done: int = 0
    frames_total: int = 0


class EventPump:
    """Background long-poller of GET /api/event.

    Usage:
        async with client.event_pump() as pump:
            await pump.wait_ready()
            ... stream the zip ...

    `wait_ready()` resolves only once the SECOND poll cycle has begun plus a
    settle delay — the Odyssey wants an actively re-polling client, not one
    that connected once (docs/API.md, "Odyssey timing quirk").
    """

    def __init__(self, http: httpx.AsyncClient, *, settle: float = 3.0) -> None:
        self._http = http
        self._settle = settle
        self._task: asyncio.Task[None] | None = None
        self._first_response = asyncio.Event()
        self._second_cycle = asyncio.Event()
        self._started = asyncio.Event()  # saw a download "started" event
        self._ended = asyncio.Event()  # saw a download "ended" event AFTER a start
        self.progress = DownloadProgress()
        self.dead: BaseException | None = None

    async def __aenter__(self) -> EventPump:
        self._task = asyncio.create_task(self._loop(), name="scopepull-event-pump")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_ended(self) -> bool:
        """True once the build-complete ("ended") signal has been seen."""
        return self._ended.is_set()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Block until the pump is actively re-polling, then settle."""
        try:
            await asyncio.wait_for(self._second_cycle.wait(), timeout)
        except TimeoutError:
            if not self.alive:
                raise PumpDead("event pump died before becoming ready") from self.dead
            # Mirror prior art: warn-and-proceed rather than hard-fail; the
            # zip attempt itself will 502 if the backend truly hasn't seen us.
            log.warning("event pump not confirmed ready after %.0fs; proceeding", timeout)
        await asyncio.sleep(self._settle)

    async def wait_for_started(self, timeout: float) -> bool:
        """Wait until the scope reports the export build has begun."""
        try:
            await asyncio.wait_for(self._started.wait(), timeout)
            return True
        except TimeoutError:
            if not self.alive:
                raise PumpDead("event pump died waiting for build start") from self.dead
            return False

    async def wait_for_ended(self, timeout: float) -> bool:
        """Wait until the scope reports the export build is complete ("ended").

        Returns True on "ended", False on timeout. Raises PumpDead if the pump
        died. Does NOT cancel anything — the build must run uninterrupted.
        """
        try:
            await asyncio.wait_for(self._ended.wait(), timeout)
            return True
        except TimeoutError:
            if not self.alive:
                raise PumpDead("event pump died waiting for build end") from self.dead
            return False

    async def _loop(self) -> None:
        first = True
        try:
            while True:
                read_timeout = FIRST_POLL_TIMEOUT if first else EVENT_POLL_TIMEOUT
                try:
                    resp = await self._http.get(
                        "/api/event",
                        timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT),
                    )
                except httpx.ReadTimeout:
                    # Timeout still means the backend held our connection.
                    self._mark_cycle(first)
                    first = False
                    continue
                except httpx.TransportError:
                    await asyncio.sleep(2)
                    continue

                self._mark_cycle(first)
                first = False
                self._handle_event(resp.text[:8192])
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # record why we died; wait_ready() surfaces it
            self.dead = e
            raise

    def _mark_cycle(self, first: bool) -> None:
        if first:
            self._first_response.set()
        elif self._first_response.is_set():
            self._second_cycle.set()

    def _handle_event(self, text: str) -> None:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        cmd = data.get("cmd", "")
        if cmd == "download":
            status = str(data.get("status", ""))
            self.progress.status = status
            if status == "started":
                self.progress.frames_done = _as_int(data.get("progress"))
                self.progress.frames_total = _as_int(data.get("nb_frames"))
                self._started.set()
            elif status == "ended":
                # "ended" is the build-complete signal, but only meaningful
                # after we've seen a matching "started" — otherwise it is the
                # idle/stale state left by a previous (or no) download.
                if self._started.is_set():
                    self._ended.set()
        elif cmd == "obslist":
            pass  # catalog changed; harmless during a pull
        else:
            log.debug("unrecognized event cmd: %r", cmd)


def _as_int(val: Any) -> int:
    return int(val) if isinstance(val, (int, float)) else 0


def _keepalive_socket_options() -> list[tuple[int, int, int]]:
    """TCP keepalive so the download connection survives a long idle build.

    A large export streams NO body bytes for many minutes while the scope
    builds the zip; without keepalive the idle socket gets reset by the USB
    Wi-Fi adapter or the scope before the bytes start (observed on M81, ~12 min
    build). SO_KEEPALIVE is universal; the interval knobs are platform-specific
    and applied only where the running Python/OS exposes them (Linux always;
    Windows 10 1709+/py3.7+; macOS uses a different name and is skipped).
    """
    opts: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 10)):
        opt = getattr(socket, name, None)
        if opt is not None:
            opts.append((socket.IPPROTO_TCP, opt, value))
    return opts


class ScopeClient:
    """Async client for one scope. Owns the httpx client; use as a context manager."""

    def __init__(
        self,
        base_url: str = "http://192.168.100.1",
        *,
        settle: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._settle = settle
        # Real connections get TCP keepalive so a long idle build (no bytes for
        # minutes) doesn't get its socket reset. Injected transports (tests'
        # ASGITransport) are left as-is.
        if transport is None:
            transport = httpx.AsyncHTTPTransport(
                socket_options=_keepalive_socket_options(),
                retries=1,
            )
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=transport,
        )

    async def __aenter__(self) -> ScopeClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- health ---------------------------------------------------------------

    async def health(self) -> bool:
        """Is anything answering at the base URL?"""
        try:
            resp = await self._http.get("/", timeout=httpx.Timeout(15, connect=5))
            return resp.status_code == 200
        except httpx.TransportError:
            return False

    async def ddd_enabled(self) -> bool:
        """Does the DDD API answer? (Exact disabled-state behavior is VERIFY-LIVE;
        for now: reachable listing endpoint returning non-HTML == enabled.)"""
        try:
            resp = await self._http.get(
                "/api/observations/list",
                timeout=httpx.Timeout(LIST_TIMEOUT, connect=CONNECT_TIMEOUT),
            )
        except httpx.TransportError as e:
            raise ScopeUnreachable(str(e)) from e
        if resp.status_code == 404:
            return False
        return "html" not in resp.headers.get("content-type", "").lower()

    # -- catalog --------------------------------------------------------------

    async def list_observations(self, *, attempts: int = 3) -> list[Observation]:
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.get(
                    "/api/observations/list",
                    timeout=httpx.Timeout(LIST_TIMEOUT, connect=CONNECT_TIMEOUT),
                )
            except httpx.TransportError as e:
                last_exc = e
                log.warning("listing attempt %d/%d failed: %s", attempt, attempts, e)
                await asyncio.sleep(min(5 * attempt, 15))
                continue

            ct = resp.headers.get("content-type", "").lower()
            if "html" in ct and "<html" in resp.text[:2048].lower():
                raise DDDNotEnabled(
                    "scope answered with the web app instead of the API — enable "
                    "Direct Data Download in the Unistellar app "
                    "(Settings → telescope → Download → Direct Data Download)"
                )
            resp.raise_for_status()
            return parse_listing(resp.text)
        raise ScopeUnreachable(f"listing failed after {attempts} attempts") from last_exc

    # -- commands -------------------------------------------------------------

    async def cancel_download(self) -> None:
        """Clear any server-side export job. Safe to call anytime; never raises
        on transport errors (used on abort paths where the link may be gone)."""
        try:
            await self._http.post(
                "/api/event",
                json={"cmd": "cancelDownload"},
                timeout=httpx.Timeout(10, connect=5),
            )
        except httpx.TransportError:
            log.debug("cancelDownload not delivered (transport error)", exc_info=True)

    # -- transfer -------------------------------------------------------------

    def stream_zip(self, url: str, *, read_timeout: float = READ_TIMEOUT) -> Any:
        """Open a streaming GET for a zip export. Use as an async context manager:

            async with client.stream_zip(url) as resp:
                async for chunk in resp.aiter_bytes(...): ...

        This single held GET both triggers the server-side build and streams the
        finished archive — the scope emits NO body bytes while it is still
        building (docs/API.md), so read_timeout must exceed the whole build
        time (scale it to the frame count). An event pump must be actively
        polling concurrently or the scope will not stream (the export gate).
        """
        return self._http.stream(
            "GET", url, timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
        )

    # -- events ---------------------------------------------------------------

    def event_pump(self) -> EventPump:
        return EventPump(self._http, settle=self._settle)
