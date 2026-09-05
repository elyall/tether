from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

typer_testing = pytest.importorskip("typer.testing")

from tether.backends.memory import default_store  # noqa: E402
from tether.cli import app  # noqa: E402

runner = typer_testing.CliRunner()


def test_cli_end_to_end(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)

    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(
        app,
        [
            "add",
            "db",
            "--kind",
            "memory",
            "--set",
            f"system={system}",
            "--branch",
            "main",
        ],
    )
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["objects"][0]["key"] == "db"
    assert payload["objects"][0]["state"] == "new"

    r = runner.invoke(app, ["commit", "-m", "baseline", "--json"])
    assert r.exit_code == 0, r.output
    commit_payload = json.loads(r.output)
    assert commit_payload["pinned"]["db"] is not None

    r = runner.invoke(app, ["new"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["open", "db", "--json"])
    assert r.exit_code == 0, r.output
    assert not json.loads(r.output)["read_only"]

    r = runner.invoke(app, ["verify", "--json"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["gc", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["dry_run"] is True
