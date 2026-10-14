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
from .archive import archive_index, combine_target, summarize
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
    LABELS,
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
    # Typer's rich traceback would otherwise swallow usage errors before run()
    # can map them to an exit code, and a boxed traceback is the wrong thing to
    # print at a caller that parses our output anyway.
    pretty_exceptions_enable=False,
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
        # soft_wrap: never fold a long line. The window and starstack read
        # these lines, and a path broken across two lines is a broken path.
        console.print(text, highlight=False, markup=False, soft_wrap=True)

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
def status(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Roots and totals only, for the window (no disk walk)"),
    ] = False,
) -> None:
    """Local archive summary, per scope."""
    cfg = Config.load()
    with Manifest() as m:
        n, total = m.totals()
        rows = m.all_observations()
    if as_json:
        # Deliberately does NOT index the archives: this is what the window
        # asks at start-up, and it has to come back instantly. Session counts
        # come from `index`, per scope, when the table is filled.
        print(
            _json.dumps(
                {
                    "version": __version__,
                    "roots": {name: str(cfg.root_for(name)) for name in SCOPE_NAMES},
                    "observations": n,
                    "bytes": total,
                    "scopes": {name: LABELS[name] for name in SCOPE_NAMES},
                }
            )
        )
        return
    # starstack parses this exact line to honour archive_root — docs/STARSTACK.md.
    # It has to stay the first line, and it is the Odyssey's root.
    console.print(f"Archive root: {cfg.archive_root}", soft_wrap=True)
    console.print(f"{n} observation(s) archived, {total / 2**30:.2f} GiB")
    if rows:
        last = rows[-1]
        console.print(f"Last pull: {last.pulled_at} ({last.target})")
    console.print()
    for name in SCOPE_NAMES:
        root = cfg.root_for(name)
        console.print(f"[bold]{LABELS[name]}[/]")
        console.print(f"  {root}")
        console.print(f"  {summarize(archive_index(root, scope=name))}")


@app.command()
def gui() -> None:
    """Open the owl: one window, one big red button (also `scopepull-gui`)."""
    # Imported here, not at the top: tkinter is missing on some minimal Linux
    # installs, and a headless box should still be able to run every other
    # command without it.
    try:
        from .gui import main as gui_main
    except ImportError as e:  # pragma: no cover - depends on the OS's tk
        err_console.print(
            f"[red]can't open the window:[/] {e}\n"
            "On Linux, tkinter is a separate package (apt install python3-tk)."
        )
        raise typer.Exit(EXIT_ERROR) from None
    gui_main()  # type: ignore[no-untyped-call]  # gui.py is mypy-excluded


@app.command()
def cancel(ip: IpOpt = None) -> None:
    """Clear a stuck server-side download job on the scope."""
    cfg = _load_config(ip)

    async def go() -> None:
        async with ScopeClient(cfg.base_url) as client:
            await client.cancel_download()

    asyncio.run(go())
    console.print("Cancel sent.")


@app.command("find")
def find_cmd(scope: ScopeOpt = None, at: AtOpt = None, ip: IpOpt = None) -> None:
    """Say where a scope is right now, and nothing else."""
    cfg = _load_config(ip)
    sc = _pick_scope(scope, cfg)
    found = sc.find(at or ip, _console_log())
    if not found:
        err_console.print(f"[red]no {sc.label} found:[/] {found.how}", soft_wrap=True)
        raise typer.Exit(found.code or EXIT_UNREACHABLE)
    console.print(f"found {sc.label} {found.how}", soft_wrap=True)


RootOpt = Annotated[
    str | None,
    typer.Option("--root", help="Use this archive folder instead of the scope's"),
]
NightsOpt = Annotated[
    str | None, typer.Option("--nights", help="Only these nights: 2026-09-22,2026-10-04")
]


@app.command("index")
def index_cmd(
    scope: ScopeOpt = None,
    root: RootOpt = None,
    all_scopes: Annotated[
        bool, typer.Option("--all-scopes", help="Every scope's archive in one listing")
    ] = False,
) -> None:
    """What the archive holds: one JSON object per session, newest night first.

    This is the window's archive table, and it is deliberately machine-readable
    — plain stdout, one object per line, no wrapping and no colour.
    """
    cfg = Config.load()
    names = list(SCOPE_NAMES) if all_scopes else [_scope_or_exit(scope)]
    for name in names:
        folder = Path(root).expanduser() if root else cfg.root_for(name)
        for session in archive_index(folder, scope=name):
            print(_json.dumps(session.as_dict()))


@app.command()
def combine(
    target: Annotated[str, typer.Argument(help="Target name, as `index` spells it")],
    scope: ScopeOpt = None,
    nights: NightsOpt = None,
    root: RootOpt = None,
) -> None:
    """Gather every night of one target into combined/<target>/ for a stacker.

    Hard links, so it costs no disk, and the nights themselves are untouched.
    """
    cfg = Config.load()
    name = _scope_or_exit(scope)
    folder = Path(root).expanduser() if root else cfg.root_for(name)
    only = [n.strip() for n in nights.split(",") if n.strip()] if nights else None
    dest = combine_target(folder, target, only, _console_log())
    if dest is None:
        raise typer.Exit(EXIT_ERROR)
    console.print(f"combined: {dest}", soft_wrap=True)


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


def _scope_or_exit(scope: str | None) -> str:
    try:
        return scope_name(scope)
    except KeyError:
        err_console.print(f"[red]Unknown scope {scope!r}[/] — known: {', '.join(SCOPE_NAMES)}")
        raise typer.Exit(EXIT_ERROR) from None


def _pick_scope(scope: str | None, cfg: Config, **opts: Any) -> Scope:
    return get_scope(_scope_or_exit(scope), cfg, **opts)


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
    name = _scope_or_exit(scope)
    if name != "odyssey" and (since or fmt or not new):
        # Say so rather than silently ignoring them: a Seestar pull is a file
        # copy, dated by filename and already in FITS, and it dedupes against
        # the archive itself, so there is nothing for --all, --since or
        # --format to do yet.
        err_console.print(
            f"[yellow]--all/--since/--format don't apply to the {LABELS[name]} — ignoring them.[/]"
        )
    sc = get_scope(name, cfg, new=new, since=since, fmt=fmt)
    log = _console_log()

    found = sc.find(at or ip, log)
    if not found:
        err_console.print(f"[red]{found.how}[/]", soft_wrap=True)
        raise typer.Exit(found.code or EXIT_UNREACHABLE)
    console.print(f"Found {sc.label} {found.how}.", soft_wrap=True)

    root = Path(dest).expanduser() if dest else sc.archive_root()
    root.mkdir(parents=True, exist_ok=True)
    res = sc.pull(found.handle, root, log, target)
    raise typer.Exit(res.code)


def run() -> None:
    """Console-script entry point, and the only place that maps an exception to
    an exit code.

    Click exits 2 on any usage error, and 2 is load-bearing in our own contract
    -- it means "nothing new on the scope, the archive is worth stacking"
    (docs/STARSTACK.md). So a mistyped flag, or a flag a newer caller passes to
    an older scopepull, used to tell starstack and the window to go ahead and
    stack on top of a pull that never ran. Usage errors exit 1 instead.

    The exceptions are matched by shape, not by class: typer vendors its own
    fork of click, so `click.UsageError` is not the class raised here, and
    which one it is has changed between typer versions.
    """
    try:
        # Not standalone: click RETURNS the exit code of a typer.Exit here
        # instead of raising it, so the return value is the contract code and
        # dropping it silently turns every "unreachable" into a success.
        rv = app(standalone_mode=False)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except BaseException as e:
        # A UsageError carries BOTH `show` and an exit_code of 2, so it has to
        # be tested first -- testing exit_code first is exactly the bug this
        # function exists to prevent.
        if callable(getattr(e, "show", None)):  # a UsageError, whoever's click it is
            e.show()  # type: ignore[attr-defined]
            raise SystemExit(EXIT_ERROR) from None
        code = getattr(e, "exit_code", None)
        if code is not None:  # typer.Exit / click's Exit: --help, --version, our own
            raise SystemExit(code) from None
        if type(e).__name__ == "Abort":
            raise SystemExit(130) from None
        raise
    raise SystemExit(rv if isinstance(rv, int) else EXIT_OK)


if __name__ == "__main__":
    run()
