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
    assert json.loads(r.output)["command"] == "gc"  # dry run prints the plan

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


def test_cli_plans_dry_run_and_from_plan(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})

    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app,
        [
            "add",
            "db",
            "--kind",
            "memory",
            "--set",
            f"system={system}",
            "--pin",
            "record",
        ],
    )
    assert r.exit_code == 0, r.output

    # -m is required unless applying a saved plan.
    r = runner.invoke(app, ["commit"])
    assert r.exit_code == 1 and "message is required" in r.output

    # Dry run prints the plan and writes nothing.
    r = runner.invoke(app, ["commit", "-m", "baseline", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert (
        "plan: commit" in r.output and "record" in r.output and "pin=record" in r.output
    )
    assert store.system(system).tags == {}
    assert not (vcs_root / ".tether" / "objects" / "db.toml").read_text().count("state")

    # --plan saves it; --from-plan applies exactly that plan.
    plan_file = vcs_root / "commit.json"
    r = runner.invoke(
        app, ["commit", "-m", "baseline", "--plan", str(plan_file), "--json"]
    )
    assert r.exit_code == 0, r.output
    saved = json.loads(plan_file.read_text())
    assert saved["command"] == "commit"
    assert [a["op"] for a in saved["actions"]] == ["record", "vcs-commit"]
    r = runner.invoke(app, ["commit", "--from-plan", str(plan_file), "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["pinned"] == {"db": None} and payload["vcs_commit"]
    assert store.system(system).tags == {}  # pin-less: still no native ref

    # A stale plan is refused (the object moved after planning).
    store.write(system, "main", {"v": 2})
    r = runner.invoke(app, ["commit", "-m", "next", "--plan", str(plan_file)])
    assert r.exit_code == 0, r.output
    store.write(system, "main", {"v": 3})
    r = runner.invoke(app, ["commit", "--from-plan", str(plan_file)])
    assert r.exit_code == 1 and "changed since the plan" in r.output

    # Wrong plan kind for the command.
    r = runner.invoke(app, ["new", "--from-plan", str(plan_file)])
    assert r.exit_code == 1 and "expected 'new'" in r.output

    # `new --dry-run` shows the pin-less fork; applying it forks from state.
    r = runner.invoke(app, ["new", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "fork" in r.output and "recorded state" in r.output
    r = runner.invoke(app, ["new", "--json"])
    assert r.exit_code == 0, r.output
    wref = json.loads(r.output)["working_refs"]["db"]
    assert wref.startswith("tether.ws.")
    assert store.read(system, wref) == {"v": 1}

    # gc: dry run by default, plan file, prune other workspaces.
    branches = store.system(system).branches
    branches["tether.ws.deadbeef.db"] = branches["main"]  # no writes: safe
    branches["tether.ws.0badf00d.db"] = branches["main"]
    store.write(system, "tether.ws.0badf00d.db", {"v": 7})  # has data: kept
    r = runner.invoke(app, ["gc"])
    assert r.exit_code == 0, r.output
    assert "deadbeef" not in r.output  # not without --prune-workspaces
    r = runner.invoke(app, ["gc", "--force-prune"])
    assert r.exit_code == 1 and "requires --prune-workspaces" in r.output
    gc_plan = vcs_root / "gc.json"
    r = runner.invoke(app, ["gc", "--prune-workspaces", "--plan", str(gc_plan)])
    assert r.exit_code == 0, r.output
    assert "delete-branch" in r.output and "tether.ws.deadbeef.db" in r.output
    assert "keep-branch" in r.output and "tether.ws.0badf00d.db" in r.output
    assert "tether.ws.deadbeef.db" in branches  # plan only
    r = runner.invoke(app, ["gc", "--from-plan", str(gc_plan), "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["deleted_working_refs"] == {"db": ["tether.ws.deadbeef.db"]}
    assert payload["kept_working_refs"] == {"db": ["tether.ws.0badf00d.db"]}
    assert "tether.ws.deadbeef.db" not in branches
    assert "tether.ws.0badf00d.db" in branches
    assert wref in branches  # ours survives
    r = runner.invoke(
        app, ["gc", "--prune-workspaces", "--force-prune", "--no-dry-run"]
    )
    assert r.exit_code == 0, r.output
    assert "tether.ws.0badf00d.db" not in branches
    assert wref in branches
