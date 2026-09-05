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

    # Content diff between the baseline and a second commit (written through
    # the forked working branch, which is what `new` made current).
    first = commit_payload["vcs_commit"]
    wref = next(b for b in default_store().system(system).branches if b != "main")
    default_store().write(system, wref, {"rows": 42})
    r = runner.invoke(app, ["commit", "-m", "more rows", "--json"])
    assert r.exit_code == 0, r.output
    second = json.loads(r.output)["vcs_commit"]

    r = runner.invoke(app, ["diff", first, second])
    assert r.exit_code == 0, r.output
    assert "changed  db" in r.output and "rows" not in r.output

    r = runner.invoke(app, ["diff", first, second, "--content"])
    assert r.exit_code == 0, r.output
    assert "[+1 -0 ~0 keys]" in r.output and "added  rows  42" in r.output

    r = runner.invoke(app, ["diff", first, second, "-c", "--json"])
    assert r.exit_code == 0, r.output
    detail = json.loads(r.output)[0]["detail"]
    assert detail["added"] == 1 and detail["entries"][0]["path"] == "rows"


def test_cli_log_and_pick(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    s1 = store.write(system, "main", {"v": 1})
    s2 = store.write(system, "main", {"v": 2})
    assert runner.invoke(app, ["init"]).exit_code == 0

    # Browse an unregistered object by kind + locator fields.
    r = runner.invoke(
        app,
        ["log", "unused", "--kind", "memory", "--set", f"system={system}", "--json"],
    )
    assert r.exit_code == 0, r.output
    ids = [e["id"] for e in json.loads(r.output)]
    assert ids[:2] == [s2, s1]

    # --at registers a detached base.
    r = runner.invoke(
        app,
        ["add", "db", "--kind", "memory", "--set", f"system={system}", "--at", s1],
    )
    assert r.exit_code == 0, r.output
    assert f"at {s1}" in r.output
    r = runner.invoke(app, ["snapshot", "--json"])
    assert json.loads(r.output)["db"] == {"snapshot_id": s1}

    # log on a registered object starts at its base (here the detached `at`);
    # --ref browses another ref. Human format: newest first with refs.
    r = runner.invoke(app, ["log", "db", "-n", "2"])
    assert r.exit_code == 0, r.output
    lines = [line for line in r.output.splitlines() if line.strip()]
    assert len(lines) == 2 and s1 in lines[0] and f"{system}:s0" in lines[1]
    r = runner.invoke(app, ["log", "db", "--ref", "main", "-n", "1"])
    assert r.exit_code == 0, r.output
    assert s2 in r.output and "[main]" in r.output

    # --pick lists numbered entries and reads the choice from stdin.
    r = runner.invoke(
        app,
        ["add", "db2", "--kind", "memory", "--set", f"system={system}", "--pick"],
        input="2\n",
    )
    assert r.exit_code == 0, r.output
    assert "  1. " in r.output and f"added db2 (memory) at {s1}" in r.output


def test_cli_snapshot_auto_config(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether.manifest import RepoConfig
    from tether.repo import Repo

    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo = Repo.init(vcs_root, config=RepoConfig(snapshot_auto=False))
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    default_store().write(system, "main", {"x": 1})

    # snapshot.auto = false: status reuses the cached fingerprint -> clean.
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["objects"][0]["state"] == "clean"
    # An explicit snapshot refreshes the cache and status sees the change.
    assert runner.invoke(app, ["snapshot"]).exit_code == 0
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.output)["objects"][0]["state"] == "modified"
