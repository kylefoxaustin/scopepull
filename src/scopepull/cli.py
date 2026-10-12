"""scopepull CLI — thin typer+rich layer over the library.

Commands: list, doctor, status, cancel, pull. `pull` goes through the Scope
protocol (scopes/__init__.py), so `--scope` picks the telescope and everything
below this layer is scope-agnostic.
Exit codes (spec §4): 0 ok, 2 nothing new, 3 scope unreachable, 4 DDD disabled,
5 partial. They are a contract — starstack and the window read them.
"""

from __future__ import annotations

import asyncio
import json as _json
import shutil
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .catalog import Observation, filter_observations
from .client import DDDNotEnabled, ScopeClient, ScopeUnreachable
from .config import Config
from .log import Log
from .manifest import Manifest
from .netcheck import check as netcheck_check
from .scopes import (
    EXIT_DDD_DISABLED,
    EXIT_ERROR,
    EXIT_NOTHING_NEW,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_UNREACHABLE,
    SCOPE_NAMES,
    Scope,
    get_scope,
    scope_name,
)

__all__ = [
    "EXIT_DDD_DISABLED",
    "EXIT_NOTHING_NEW",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_UNREACHABLE",
    "app",
]

app = typer.Typer(
    name="scopepull",
    help="Gets the night off your smart scope: Unistellar Odyssey Pro, ZWO Seestar.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _load_config(ip: str | None) -> Config:
    cfg = Config.load()
    if ip:
        cfg.scope_ip = ip
    return cfg


def _console_log() -> Log:
    """The owl's voice on a terminal.

    Plain lines (no rich markup: the same text goes to the window and to log
    files) printed through rich so odd consoles still get the encoding right,
    plus the raw channel for the in-place progress line.
    """

    def write(text: str) -> None:
        console.print(text, highlight=False, markup=False)

    def raw(text: str) -> None:
        if text.endswith("\r"):
            console.print(text[:-1], end="\r", highlight=False, markup=False, soft_wrap=True)
        elif text == "\n":
            console.print()
        else:
            console.print(text, end="", highlight=False, markup=False, soft_wrap=True)

    return Log(write, raw)


IpOpt = Annotated[str | None, typer.Option("--ip", help="Scope IP (default from config)")]
ScopeOpt = Annotated[
    str | None,
    typer.Option("--scope", help=f"Which telescope: {' | '.join(SCOPE_NAMES)} (default odyssey)"),
]
AtOpt = Annotated[
    str | None,
    typer.Option(
        "--at",
        help="Where the scope is: an IP (Odyssey) or a drive/share/host (Seestar)",
        metavar="SPEC",
    ),
]


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", is_eager=True)] = False,
) -> None:
    if version:
        console.print(f"scopepull {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Bare `scopepull` == `scopepull pull --new`, the Odyssey, as always.
        _run_pull(
            scope=None, new=True, ip=None, at=None, since=None, target=None, fmt=None, dest=None
        )


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

    observations = filter_observations(observations, since, target)
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
    # starstack parses this exact line to honour archive_root — docs/STARSTACK.md.
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
    scope: ScopeOpt = None,
    ip: IpOpt = None,
    at: AtOpt = None,
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
    """Pull everything the archive hasn't got, verify, and ingest it."""
    _run_pull(scope=scope, new=new, ip=ip, at=at, since=since, target=target, fmt=fmt, dest=dest)


def _pick_scope(scope: str | None, cfg: Config, **opts: Any) -> Scope:
    try:
        name = scope_name(scope)
    except KeyError:
        err_console.print(f"[red]Unknown scope {scope!r}[/] — known: {', '.join(SCOPE_NAMES)}")
        raise typer.Exit(EXIT_ERROR) from None
    return get_scope(name, cfg, **opts)


def _run_pull(
    *,
    scope: str | None,
    new: bool,
    ip: str | None,
    at: str | None,
    since: str | None,
    target: str | None,
    fmt: str | None,
    dest: str | None,
) -> None:
    cfg = _load_config(ip)
    sc = _pick_scope(scope, cfg, new=new, since=since, fmt=fmt)
    log = _console_log()

    found = sc.find(at or ip, log)
    if not found:
        err_console.print(f"[red]{found.how}[/]")
        raise typer.Exit(found.code or EXIT_UNREACHABLE)
    console.print(f"Found {sc.label} {found.how}.")

    root = Path(dest).expanduser() if dest else sc.archive_root()
    root.mkdir(parents=True, exist_ok=True)
    res = sc.pull(found.handle, root, log, target)
    raise typer.Exit(res.code)


if __name__ == "__main__":
    app()
