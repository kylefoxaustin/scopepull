"""CLI wiring: app imports, all commands registered with evaluable annotations."""

from __future__ import annotations

from typer.main import get_command
from typer.testing import CliRunner

from scopepull.cli import app

runner = CliRunner()


def test_app_help_lists_commands():
    # Forces typer to evaluate every command's annotations (guards against the
    # missing-import class of bug, e.g. Optional not imported).
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("pull", "list", "doctor", "status", "cancel"):
        assert cmd in result.output


def test_every_command_builds():
    # get_command materializes all params/annotations for each subcommand.
    click_cmd = get_command(app)
    names = set(click_cmd.commands)  # type: ignore[attr-defined]
    assert {"pull", "list", "doctor", "status", "cancel"} <= names


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "scopepull" in result.output


def test_reuse_zip_yields_done_without_a_scope(tmp_path):
    """A valid zip left in _incoming (ingest failed last run) is reused: the
    stand-in yields the same phases transfer_pull ends with, no download."""
    import asyncio

    from scopepull.catalog import Observation
    from scopepull.cli import _reuse_zip
    from tests.mock_scope import OBSERVATIONS, make_calibration_zip

    z = tmp_path / "x.zip"
    z.write_bytes(make_calibration_zip(OBSERVATIONS[0]))
    obs = Observation.from_raw(OBSERVATIONS[0])

    async def phases():
        return [ev.phase async for ev in _reuse_zip(obs, z)]

    assert asyncio.run(phases()) == ["already downloaded", "done"]


def test_ingest_failure_is_partial_not_fatal(monkeypatch, tmp_path):
    """One observation whose ingest raises must mark that one failed, keep its
    zip for next time, carry on with the rest, and exit 5 -- not crash with
    exit 1 (0.1.1 did, on a NaN in one manifest)."""
    import scopepull.cli as cli
    from scopepull.catalog import Observation
    from scopepull.transfer import ProgressEvent
    from tests.mock_scope import OBSERVATIONS, make_calibration_zip

    obs_list = [Observation.from_raw(o) for o in OBSERVATIONS[:2]]

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def list_observations(self):
            return obs_list

    async def fake_pull(client, obs, zip_path, *, fmt):
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(make_calibration_zip(obs.raw))
        yield ProgressEvent(obs.obs_id, "done", bytes_done=zip_path.stat().st_size)

    calls = []

    def fake_ingest(zip_path, obs, cfg, m):
        calls.append(obs.target)
        if len(calls) == 1:
            raise ValueError("Floating point nan values are not allowed in FITS headers.")
        return type("R", (), {"light_count": 2, "fits_written": 3})()

    class FakeManifest:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def local_ids(self):
            return set()

    monkeypatch.setattr(cli, "ScopeClient", FakeClient)
    monkeypatch.setattr(cli, "transfer_pull", fake_pull)
    monkeypatch.setattr(cli, "ingest_zip", fake_ingest)
    monkeypatch.setattr(cli, "Manifest", FakeManifest)
    monkeypatch.setattr(
        cli, "_load_config", lambda ip: cli.Config(archive_root=tmp_path / "arch", format="tiff")
    )

    r = runner.invoke(cli.app, ["pull", "--new"])
    assert r.exit_code == 5, r.output
    assert calls == [obs_list[0].target, obs_list[1].target]  # carried on after the failure
    assert "ingest failed" in r.output and "nan" in r.output
    kept = list((tmp_path / "arch" / "_incoming").glob("*.zip"))
    assert len(kept) == 1  # the failed one's zip stays for reuse
