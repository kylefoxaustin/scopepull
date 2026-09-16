"""scopepull CLI — thin typer+rich layer over the library.

Phase 1 surface: list, doctor, status, cancel. pull/pick arrive in Phases 2-3.
Exit codes (spec §4): 0 ok, 2 nothing new, 3 scope unreachable, 4 DDD disabled.
"""

from __future__ import annotations

import asyncio
import json as _json
import shutil
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .catalog import Observation
from .client import DDDNotEnabled, ScopeClient, ScopeUnreachable
from .config import Config
from .fsutil import unlink_retry
from .ingest import ingest_zip
from .manifest import Manifest
from .netcheck import check as netcheck_check
from .transfer import ProgressEvent, TransferError, zip_has_frames
from .transfer import pull as transfer_pull

app = typer.Typer(
    name="scopepull",
    help="One-shot stack puller for Unistellar Odyssey Pro telescopes.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_NOTHING_NEW = 2
EXIT_UNREACHABLE = 3
EXIT_DDD_DISABLED = 4
EXIT_PARTIAL = 5


def _load_config(ip: str | None) -> Config:
    cfg = Config.load()
    if ip:
        cfg.scope_ip = ip
    return cfg


IpOpt = Annotated[str | None, typer.Option("--ip", help="Scope IP (default from config)")]


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
) -> None:
    if version:
        console.print(f"scopepull {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Bare `scopepull` == `scopepull pull --new`.
        _pull(new=True, all_=False, ip=None, since=None, target=None, fmt=None, dest=None)


@app.command("list")
def list_cmd(
    ip: IpOpt = None,
    since: Annotated[
        str | None, typer.Option(help="Only observations on/after DATE (YYYY-MM-DD)")
    ] = None,
    target: Annotated[str | None, typer.Option(help="Filter by target substring")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """List observations on the scope and whether each is already local."""
    cfg = _load_config(ip)

    async def go() -> list[Observation]:
        async with ScopeClient(cfg.base_url) as client:
            return await client.list_observations()

    try:
        observations = asyncio.run(go())
    except ScopeUnreachable as e:
        err_console.print(f"[red]Scope unreachable:[/] {e}")
        raise typer.Exit(EXIT_UNREACHABLE) from None
    except DDDNotEnabled as e:
        err_console.print(f"[red]{e}[/]")
        raise typer.Exit(EXIT_DDD_DISABLED) from None

    observations = _filter(observations, since, target)
    with Manifest() as m:
        local = m.local_ids()

    if as_json:
        payload = [
            {
                "id": o.obs_id,
                "target": o.target,
                "mode": f"{o.pmode} {o.purpose}".strip(),
                "started_at": o.started_at.isoformat() if o.started_at else None,
                "frames": o.frame_count,
                "local": o.obs_id in local,
            }
            for o in observations
        ]
        console.print_json(_json.dumps(payload))
        return

    table = Table(title=f"Observations on {cfg.scope_ip}")
    table.add_column("Date")
    table.add_column("Target")
    table.add_column("Mode")
    table.add_column("Frames", justify="right")
    table.add_column("Local?", justify="center")
    for o in sorted(observations, key=lambda o: (o.started_at is not None, o.started_at)):
        table.add_row(
            o.date_str,
            o.target,
            f"{o.pmode} {o.purpose}".strip(),
            str(o.frame_count),
            "✓" if o.obs_id in local else "—",
        )
    console.print(table)
    new = sum(1 for o in observations if o.obs_id not in local)
    console.print(f"{len(observations)} observation(s), [bold]{new} not yet local[/]")


def _filter(
    observations: list[Observation], since: str | None, target: str | None
) -> list[Observation]:
    if since:
        cutoff = datetime.fromisoformat(since).astimezone()
        observations = [
            o for o in observations if o.started_at and o.started_at.astimezone() >= cutoff
        ]
    if target:
        t = target.lower()
        observations = [o for o in observations if t in o.target.lower() or t in o.name.lower()]
    return observations


@app.command()
def doctor(ip: IpOpt = None) -> None:
    """Check connectivity, DDD state, and local disk before pulling."""
    cfg = _load_config(ip)
    ok = True

    async def go() -> int:
        nonlocal ok
        net = await netcheck_check(cfg.scope_ip)
        _report("Wi-Fi", bool(net.ssids), ", ".join(net.ssids) or "no Wi-Fi detected")
        _report("Scope reachable", net.reachable, cfg.base_url)
        if not net.reachable:
            err_console.print(f"  → {net.diagnosis(cfg.scope_ip)}")
            ok = False
            return EXIT_UNREACHABLE

        async with ScopeClient(cfg.base_url) as client:
            try:
                ddd = await client.ddd_enabled()
            except ScopeUnreachable:
                ddd = False
            _report(
                "Direct Data Download",
                ddd,
                "" if ddd else "enable in Unistellar app: Settings → telescope → Download",
            )
            if not ddd:
                ok = False
                return EXIT_DDD_DISABLED

        free = shutil.disk_usage(
            cfg.archive_root
            if cfg.archive_root.exists()
            else cfg.archive_root.parent
            if cfg.archive_root.parent.exists()
            else "."
        ).free
        _report("Free disk (archive root)", free > 5 * 2**30, f"{free / 2**30:.1f} GiB free")
        return EXIT_OK

    code = asyncio.run(go())
    console.print("[green]All good.[/]" if ok else "[red]Problems found.[/]")
    raise typer.Exit(code)


def _report(label: str, good: bool, detail: str) -> None:
    mark = "[green]✓[/]" if good else "[red]✗[/]"
    console.print(f" {mark} {label:<28} {detail}")


@app.command()
def status() -> None:
    """Local archive summary."""
    cfg = Config.load()
    with Manifest() as m:
        n, total = m.totals()
        rows = m.all_observations()
    console.print(f"Archive root: {cfg.archive_root}")
    console.print(f"{n} observation(s) archived, {total / 2**30:.2f} GiB")
    if rows:
        last = rows[-1]
        console.print(f"Last pull: {last.pulled_at} ({last.target})")


@app.command()
def cancel(ip: IpOpt = None) -> None:
    """Clear a stuck server-side download job on the scope."""
    cfg = _load_config(ip)

    async def go() -> None:
        async with ScopeClient(cfg.base_url) as client:
            await client.cancel_download()

    asyncio.run(go())
    console.print("Cancel sent.")


@app.command()
def pull(
    ip: IpOpt = None,
    new: Annotated[
        bool,
        typer.Option(
            "--new/--all", help="Only observations not already local (default) / everything"
        ),
    ] = True,
    since: Annotated[str | None, typer.Option(help="On/after DATE (YYYY-MM-DD)")] = None,
    target: Annotated[str | None, typer.Option(help="Filter by target substring")] = None,
    fmt: Annotated[
        str | None, typer.Option("--format", help="fits|tiff|png (default from config)")
    ] = None,
    dest: Annotated[str | None, typer.Option(help="Archive root override")] = None,
) -> None:
    """Pull observations, verify, and ingest into the archive."""
    _pull(new=new, all_=not new, ip=ip, since=since, target=target, fmt=fmt, dest=dest)


async def _reuse_zip(obs: Observation, zip_path: Path) -> AsyncIterator[ProgressEvent]:
    """Stand-in for transfer_pull when the zip is already here and valid."""
    n = zip_path.stat().st_size
    yield ProgressEvent(obs.obs_id, "already downloaded", bytes_done=n)
    yield ProgressEvent(obs.obs_id, "done", bytes_done=n)


def _build_status(ev: object) -> str:
    """Live line: 'building 187/364 frames · 3m20s' while building, then
    'downloading 210MB · 512KB/s' once the archive streams."""
    done = getattr(ev, "frames_done", 0)
    total = getattr(ev, "frames_total", 0)
    elapsed = getattr(ev, "elapsed_s", 0.0)
    nbytes = getattr(ev, "bytes_done", 0)

    def _dur(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"

    # No body bytes flow until the build finishes, so bytes>threshold == streaming.
    if nbytes > 20_000:
        mb = nbytes / 2**20
        rate = nbytes / elapsed if elapsed > 0 else 0
        rate_s = f"{rate / 2**20:.1f}MB/s" if rate > 2**20 else f"{rate / 1024:.0f}KB/s"
        return f"downloading {mb:.0f}MB · {rate_s}"
    parts = []
    if total:
        parts.append(f"building {done}/{total} frames")
    else:
        parts.append("building")
    parts.append(_dur(elapsed))
    if done and total and elapsed > 2 and done < total:
        rate = done / elapsed
        if rate > 0:
            parts.append(f"~{_dur((total - done) / rate)} left")
    return " · ".join(parts)


def _pull(
    *,
    new: bool,
    all_: bool,
    ip: str | None,
    since: str | None,
    target: str | None,
    fmt: str | None,
    dest: str | None,
) -> None:
    cfg = _load_config(ip)
    if fmt:
        cfg.format = fmt
    if dest:
        from pathlib import Path

        cfg.archive_root = Path(dest).expanduser()

    async def go() -> int:
        async with ScopeClient(cfg.base_url) as client:
            try:
                observations = await client.list_observations()
            except ScopeUnreachable as e:
                err_console.print(f"[red]Scope unreachable:[/] {e}")
                return EXIT_UNREACHABLE
            except DDDNotEnabled as e:
                err_console.print(f"[red]{e}[/]")
                return EXIT_DDD_DISABLED

            observations = _filter(observations, since, target)
            with Manifest() as m:
                if new:
                    local = m.local_ids()
                    observations = [o for o in observations if o.obs_id not in local]
                if not observations:
                    console.print("[green]Nothing new to pull.[/]")
                    return EXIT_NOTHING_NEW

                console.print(f"Pulling {len(observations)} observation(s) to {cfg.archive_root}")
                failed: list[str] = []
                tmp_root = cfg.archive_root / "_incoming"
                for i, obs in enumerate(observations, 1):
                    zip_path = tmp_root / f"{obs.id_short}.zip"
                    label = f"[{i}/{len(observations)}] {obs.target}"
                    try:
                        last = ""
                        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                        tick = 0
                        on_progress_line = False
                        # A complete, frame-bearing zip left over from a run that died
                        # after the download (ingest failure, crash) is reused, not
                        # re-fetched: 1.2 GB of NGC 7217 should be pulled once.
                        events = (
                            _reuse_zip(obs, zip_path)
                            if zip_path.exists() and zip_has_frames(zip_path)
                            else transfer_pull(client, obs, zip_path, fmt=cfg.format)
                        )
                        async for ev in events:
                            if ev.phase == "downloading":
                                tick += 1
                                # ljust pads over any longer previous line so it
                                # doesn't leave residue (the "donewnloading" bug).
                                line = f"  {label}: {spin[tick % len(spin)]} {_build_status(ev)}"
                                console.print(line.ljust(72), end="\r", highlight=False)
                                on_progress_line = True
                            elif ev.phase != last:
                                if on_progress_line:
                                    console.print()  # finalize the in-place line
                                    on_progress_line = False
                                console.print(f"  {label}: {ev.phase}")
                            last = ev.phase
                        if on_progress_line:
                            console.print()
                        try:
                            res = ingest_zip(zip_path, obs, cfg, m)
                        except Exception as e:  # one bad manifest must not end the night
                            err_console.print(
                                f"  {label}: [red]ingest failed[/] — {type(e).__name__}: {e} "
                                f"(zip kept at {zip_path}; retried next run)"
                            )
                            failed.append(obs.target)
                            continue
                        unlink_retry(zip_path)
                        console.print(
                            f"  {label}: [green]ingested[/] "
                            f"{res.light_count} frames, {res.fits_written} FITS"
                        )
                    except TransferError as e:
                        err_console.print(f"  {label}: [red]failed[/] — {e}")
                        failed.append(obs.target)
                        unlink_retry(zip_path)

                if failed:
                    err_console.print(
                        f"[yellow]{len(failed)} failed (retry next run): {', '.join(failed)}[/]"
                    )
                    return EXIT_PARTIAL
                return EXIT_OK

    raise typer.Exit(asyncio.run(go()))


if __name__ == "__main__":
    app()
