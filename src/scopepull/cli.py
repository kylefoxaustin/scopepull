"""scopepull CLI — thin typer+rich layer over the library.

Phase 1 surface: list, doctor, status, cancel. pull/pick arrive in Phases 2-3.
Exit codes (spec §4): 0 ok, 2 nothing new, 3 scope unreachable, 4 DDD disabled.
"""

from __future__ import annotations

import asyncio
import json as _json
import shutil
from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .catalog import Observation
from .client import DDDNotEnabled, ScopeClient, ScopeUnreachable
from .config import Config
from .manifest import Manifest
from .netcheck import check as netcheck_check

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
        # `scopepull` == `scopepull pull --new` (Phase 2). Until then, guide.
        console.print("[yellow]pull is not implemented yet (Phase 2) — try:[/] scopepull list")
        raise typer.Exit(EXIT_OK)


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


if __name__ == "__main__":
    app()
