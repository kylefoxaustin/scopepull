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
