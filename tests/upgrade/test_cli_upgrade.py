"""`tether upgrade` through the CLI. Lives with the upgrade tests: the whole
directory goes when `tether.upgrade` is removed at 0.1.0."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tether.backends.memory import default_store
from tether.cli import app
from tether.manifest import CONFIG_VERSION

runner = CliRunner()


def test_cli_upgrade(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_upgrade import _v1_dataset

    monkeypatch.chdir(vcs_root)
    system, _s1, _s2, pin1, _pin2 = _v1_dataset(vcs_root)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1 and "tether upgrade" in r.output
    r = runner.invoke(app, ["upgrade", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "rename-pin" in r.output and "rewrite-history" in r.output
    assert f"tether.{pin1}" in default_store().system(system).tags  # dry run
    r = runner.invoke(app, ["upgrade", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["from_version"] == 1 and payload["to_version"] == CONFIG_VERSION
    assert f"tether.{pin1}" in payload["renamed_pins"] and payload["vcs_commit"]
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["upgrade"])
    assert r.exit_code == 0 and f"already at version {CONFIG_VERSION}" in r.output
