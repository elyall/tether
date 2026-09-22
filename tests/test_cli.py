from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

typer_testing = pytest.importorskip("typer.testing")

from tether.backends.memory import default_store  # noqa: E402
from tether.cli import app  # noqa: E402
from tether.repo import Repo  # noqa: E402

runner = typer_testing.CliRunner()


def _pg(dsn: str) -> Any:
    """A psycopg connection, untyped: tests run dynamic SQL ty cannot check."""
    import psycopg

    return psycopg.connect(dsn)


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

    r = runner.invoke(app, ["new", "-b", "work"])
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


def test_cli_status_is_local_by_default(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tether.manifest import RepoConfig
    from tether.repo import Repo

    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo = Repo.init(vcs_root)
    assert repo.config.snapshot_auto is False
    repo.add("db", "memory", {"system": system, "branch": "main"})

    # No snapshot yet: status takes one (there is nothing else to show).
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    first = json.loads(r.output)
    assert first["fresh"] is True and first["snapshot_at"]
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0
    default_store().write(system, "main", {"x": 1})

    # Default: the cached states, with their age, and nothing contacted.
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "states as fingerprinted" in r.output and "--snapshot to refresh" in r.output
    r = runner.invoke(app, ["status", "--json"])
    payload = json.loads(r.output)
    assert payload["fresh"] is False and payload["objects"][0]["state"] == "clean"
    # --snapshot fans out and sees main moved (on the trunk main is the
    # working ref); the age line disappears.
    r = runner.invoke(app, ["status", "--snapshot"])
    assert r.exit_code == 0 and "modified" in r.output
    assert "states as fingerprinted" not in r.output
    r = runner.invoke(app, ["status", "--json"])  # cache refreshed by the fan-out
    assert json.loads(r.output)["objects"][0]["state"] == "modified"

    # commit fingerprints the working refs, whatever [snapshot] auto says.
    default_store().write(system, "main", {"x": 2})
    r = runner.invoke(app, ["commit", "-m", "second", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["pinned"]["db"]

    # [snapshot] auto = true restores fan-out on every status.
    from tether.manifest import write_config

    write_config(
        vcs_root, RepoConfig(dataset_id=repo.config.dataset_id, snapshot_auto=True)
    )
    default_store().write(system, "main", {"x": 3})
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.output)["fresh"] is True
    assert json.loads(r.output)["objects"][0]["state"] == "modified"
    # ... and --no-snapshot still wins.
    r = runner.invoke(app, ["status", "--no-snapshot", "--json"])
    assert json.loads(r.output)["fresh"] is False


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
    r = runner.invoke(app, ["new", "-b", "work", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "fork" in r.output and "recorded state" in r.output
    r = runner.invoke(app, ["new", "-b", "work", "--json"])
    assert r.exit_code == 0, r.output
    wref = json.loads(r.output)["working_refs"]["db"]
    assert wref.startswith("tether.ws.")
    assert store.read(system, wref) == {"v": 1}

    # gc: dry run by default, plan file, prune other workspaces.
    branches = store.system(system).branches
    ds = Repo.find(vcs_root).config.dataset_id
    branches[f"tether.ws.{ds}.deadbeef"] = branches["main"]  # no writes: safe
    branches[f"tether.ws.{ds}.0badf00d"] = branches["main"]
    store.write(system, f"tether.ws.{ds}.0badf00d", {"v": 7})  # has data: kept
    # Another dataset's branch in the same store: never gc's business.
    branches["tether.ws.ffffffff.deadbeef"] = branches["main"]
    r = runner.invoke(app, ["gc"])
    assert r.exit_code == 0, r.output
    assert "deadbeef" not in r.output  # not without --prune-bookmarks
    r = runner.invoke(app, ["gc", "--force-prune"])
    assert r.exit_code == 1 and "requires --prune-bookmarks" in r.output
    gc_plan = vcs_root / "gc.json"
    r = runner.invoke(app, ["gc", "--prune-bookmarks", "--plan", str(gc_plan)])
    assert r.exit_code == 0, r.output
    assert "delete-branch" in r.output and f"tether.ws.{ds}.deadbeef" in r.output
    assert "keep-branch" in r.output and f"tether.ws.{ds}.0badf00d" in r.output
    assert f"tether.ws.{ds}.deadbeef" in branches  # plan only
    r = runner.invoke(app, ["gc", "--from-plan", str(gc_plan), "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["deleted_working_refs"] == {"db": [f"tether.ws.{ds}.deadbeef"]}
    assert payload["kept_working_refs"] == {"db": [f"tether.ws.{ds}.0badf00d"]}
    assert f"tether.ws.{ds}.deadbeef" not in branches
    assert f"tether.ws.{ds}.0badf00d" in branches
    assert "tether.ws.ffffffff.deadbeef" in branches  # foreign, untouched
    assert wref in branches  # ours survives
    r = runner.invoke(app, ["gc", "--prune-bookmarks", "--force-prune", "--no-dry-run"])
    assert r.exit_code == 0, r.output
    assert f"tether.ws.{ds}.0badf00d" not in branches
    assert wref in branches


def test_cli_export_publish_import(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csv
    import sqlite3

    monkeypatch.chdir(vcs_root)
    monkeypatch.delenv("TETHER_PUBLISH_DSN", raising=False)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0

    # export: sqlite (default) and a jsonl directory.
    r = runner.invoke(app, ["export", "out.sqlite", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["format"] == "sqlite" and payload["rows"]["objects"] >= 1
    con = sqlite3.connect(vcs_root / "out.sqlite")
    assert con.execute("SELECT COUNT(*) FROM objects_head").fetchone()[0] == 1
    con.close()
    r = runner.invoke(app, ["export", "dump", "--format", "jsonl"])
    assert r.exit_code == 0, r.output
    assert (vcs_root / "dump" / "objects.jsonl").exists()
    assert runner.invoke(app, ["export", "x", "--format", "xlsx"]).exit_code == 1

    # publish: refuses without a DSN (never read from tether.toml).
    r = runner.invoke(app, ["publish", "--dry-run"])
    assert r.exit_code == 1 and "DSN is required" in r.output

    # import: from a CSV registry, dry run first, then apply, then sync.
    reg = vcs_root / "registry.csv"
    with reg.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key", "kind", "locator_json", "policy_pin"])
        w.writeheader()
        w.writerow(
            {
                "key": "db",
                "kind": "memory",
                "locator_json": json.dumps({"system": system}),
                "policy_pin": "native",
            }
        )
        other = f"sys-{uuid.uuid4().hex[:8]}"
        store.system(other)
        w.writerow(
            {
                "key": "scratch/other",
                "kind": "memory",
                "locator_json": json.dumps({"system": other}),
                "policy_pin": "record",
            }
        )
    r = runner.invoke(app, ["import", str(reg), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert (
        "add" in r.output
        and "scratch/other" in r.output
        and "db: unchanged" in r.output
    )
    assert "scratch/other" not in json.dumps(list(Repo.find(".").objects))
    r = runner.invoke(app, ["import", str(reg), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {
        "added": ["scratch/other"],
        "updated": [],
        "removed": [],
        "unchanged": ["db"],
    }
    assert Repo.find(".").objects["scratch/other"].policy.pin == "record"

    # A SQL source with the query saved in secrets.toml; --sync removes the rest.
    db = vcs_root / "reg.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE data_object (name TEXT, sys TEXT)")
    con.execute("INSERT INTO data_object VALUES ('db', ?)", (system,))
    con.commit()
    con.close()
    secrets = vcs_root / ".tether" / "secrets.toml"
    secrets.write_text(
        "[import]\nquery = \"SELECT name AS key, 'memory' AS kind, "
        "json_object('system', sys) AS locator_json FROM data_object\"\n"
    )
    secrets.chmod(0o600)
    r = runner.invoke(app, ["import", str(db), "--sync", "--plan", "imp.json"])
    assert r.exit_code == 0, r.output
    assert "remove" in r.output and "scratch/other" in r.output
    assert "scratch/other" in Repo.find(".").objects  # plan only
    r = runner.invoke(app, ["import", str(db), "--from-plan", "imp.json", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["removed"] == ["scratch/other"]
    assert "scratch/other" not in Repo.find(".").objects


def test_cli_publish_and_import_postgres(
    vcs_root: Path, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0

    # --dry-run plans per-table counts and creates nothing.
    r = runner.invoke(app, ["publish", "--to", pg_dsn, "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "plan: publish" in r.output and "tether.objects" in r.output
    with _pg(pg_dsn) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM information_schema.schemata "
                "WHERE schema_name = 'tether'"
            ).fetchone()[0]
            == 0
        )

    # The DSN can come from the environment; the second run skips known commits.
    monkeypatch.setenv("TETHER_PUBLISH_DSN", pg_dsn)
    r = runner.invoke(app, ["publish", "--json"])
    assert r.exit_code == 0, r.output
    first = json.loads(r.output)
    assert first["schema"] == "tether" and first["upserted"]["objects"] >= 1
    r = runner.invoke(app, ["publish", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["skipped_commits"] == first["upserted"]["commits"]

    # Import from the published tables through a query.
    with _pg(pg_dsn) as conn:
        conn.execute(
            "CREATE TABLE registry AS "
            "SELECT key, kind, locator_json FROM tether.objects_head"
        )
        other = f"sys-{uuid.uuid4().hex[:8]}"
        default_store().system(other)
        conn.execute(
            "INSERT INTO registry VALUES ('scratch/two', 'memory', %s::jsonb)",
            (json.dumps({"system": other}),),
        )
        conn.commit()
    r = runner.invoke(app, ["import", pg_dsn, "--table", "registry", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {
        "added": ["scratch/two"],
        "updated": [],
        "removed": [],
        "unchanged": ["db"],
    }
    assert "scratch/two" in Repo.find(".").objects


def test_cli_promote(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"a": 1})
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0
    r = runner.invoke(app, ["new", "-b", "work", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["pending_forks"]["db"].startswith("tether.ws.")
    assert runner.invoke(app, ["open", "db"]).exit_code == 0  # materializes the fork
    wref = Repo.find(".").workspace.working_refs["db"]
    s2 = store.write(system, wref, {"a": 1, "b": 2})

    # Only committed states land: the trunk moves to the bookmark's commit,
    # whose manifest must describe the upstream afterwards.
    r = runner.invoke(app, ["promote", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "refuse" in r.output and "`tether commit` them first" in r.output
    assert runner.invoke(app, ["commit", "-m", "work"]).exit_code == 0
    r = runner.invoke(app, ["promote", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "fast-forward" in r.output and "base unchanged since fork" in r.output
    assert store.system(system).branches["main"] != s2
    r = runner.invoke(app, ["promote", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["fast_forwarded"] == {"db": {"snapshot_id": s2}}
    assert store.system(system).branches["main"] == s2

    # A divergence with the ff strategy is refused (non-zero exit, hint shown).
    store.write(system, wref, {"a": 1, "b": 2, "c": 3})
    assert runner.invoke(app, ["commit", "-m", "more work"]).exit_code == 0
    store.write(system, "main", {"a": 7, "b": 2})
    r = runner.invoke(app, ["promote", "--strategy", "ff"])
    assert r.exit_code == 1, r.output
    assert "refused" in r.output and "strategy=ff" in r.output
    r = runner.invoke(app, ["promote", "-m", "merge it"])
    assert r.exit_code == 0, r.output
    assert "merged" in r.output and "tether commit" in r.output
    assert store.read(system, "main") == {"a": 7, "b": 2, "c": 3}
    r = runner.invoke(app, ["promote", "--strategy", "sideways"])
    assert r.exit_code == 1


def test_cli_ops(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "dataset id" in r.output
    r = runner.invoke(app, ["ops"])
    assert r.exit_code == 0 and "no operations recorded" in r.output
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["commit", "-m", "baseline"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["ops"])
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert (
        len(lines) == 2 and lines[0].split()[2] == "commit" and "pinned db" in lines[0]
    )
    assert lines[1].split()[2] == "add"
    r = runner.invoke(app, ["ops", "-n", "1", "--json"])
    payload = json.loads(r.output)
    assert len(payload) == 1 and payload[0]["command"] == "commit"
    assert payload[0]["result"]["pinned"]["db"] and payload[0]["pre"]["objects"]["db"]


def test_cli_undo(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0
    r = runner.invoke(app, ["new", "-b", "work", "--eager", "--json"])
    assert r.exit_code == 0, r.output
    wref = json.loads(r.output)["working_refs"]["db"]
    assert wref in store.system(system).branches

    r = runner.invoke(app, ["undo"])
    assert r.exit_code == 0, r.output
    assert "undid" in r.output and "(new" in r.output and f"deleted {wref}" in r.output
    assert wref not in store.system(system).branches

    r = runner.invoke(app, ["undo", "--json"])  # the commit: uncommit
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["command"] == "commit" and payload["irreversible"] == []
    r = runner.invoke(app, ["ops"])
    lines = r.output.strip().splitlines()
    assert [ln.split()[2] for ln in lines] == ["undo", "undo", "new", "commit", "add"]
    assert lines[2].endswith(f"(undone by {lines[1].split()[0]})")  # new
    assert lines[3].endswith(f"(undone by {lines[0].split()[0]})")  # commit

    r = runner.invoke(app, ["undo", "nope0000dead"])
    assert r.exit_code == 1 and "no operation" in r.output
    # A partial undo exits 2 and says what could not be reversed.
    assert runner.invoke(app, ["commit", "-m", "again"]).exit_code == 0
    backend = Repo.find(vcs_root).backend_for("memory")
    ds = Repo.find(vcs_root).config.dataset_id
    backend.pin(
        {"system": system},
        {"snapshot_id": store.system(system).branches["main"]},
        f"{ds}.0000000000badbad",
    )
    stray = f"tether.ws.{ds}.deadbeef.db-000000"
    store.system(system).branches[stray] = store.system(system).branches["main"]
    # The pin was made by hand, not by a commit of this clone: plain gc keeps
    # it (`keep-pin`); `--release-foreign` is the opt-in.
    r = runner.invoke(app, ["gc", "--prune-bookmarks"])
    assert r.exit_code == 0, r.output
    assert "keep-pin" in r.output and "not created by this clone" in r.output
    r = runner.invoke(
        app, ["gc", "--no-dry-run", "--prune-bookmarks", "--release-foreign"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["undo"])
    assert r.exit_code == 2, r.output
    assert "IRREVERSIBLE" in r.output and "pin(s) deleted" in r.output
    assert "branch(es) deleted" in r.output and "repair" in r.output
    assert stray not in store.system(system).branches  # gc's deletions stand


def test_cli_repair(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0
    pin = Repo.find(vcs_root).objects["db"].pin
    assert pin is not None
    r = runner.invoke(app, ["repair", "--dry-run"])
    assert r.exit_code == 0 and "nothing to repair" in r.output

    del store.system(system).tags[pin.ref]
    r = runner.invoke(app, ["verify", "--json"])
    assert r.exit_code != 0 or "missing" in r.output.lower()
    r = runner.invoke(app, ["repair", "--dry-run"])
    assert r.exit_code == 0 and "repin" in r.output and pin.ref in r.output
    assert pin.ref not in store.system(system).tags  # dry run
    r = runner.invoke(app, ["repair", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["repinned"] == {"db": pin.id}
    assert pin.ref in store.system(system).tags


def test_cli_abandon(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "v1"]).exit_code == 0
    store.write(system, "main", {"v": 2})
    r = runner.invoke(app, ["commit", "-m", "v2", "--json"])
    assert r.exit_code == 0, r.output
    c2 = json.loads(r.output)["vcs_commit"]
    p2 = json.loads(r.output)["pinned"]["db"]
    store.write(system, "main", {"v": 3})
    assert runner.invoke(app, ["commit", "-m", "v3"]).exit_code == 0

    r = runner.invoke(app, ["abandon", c2])
    assert r.exit_code == 0, r.output
    assert f"abandoned {c2[:12]}" in r.output and "unpin" in r.output and p2 in r.output
    assert "not applied" in r.output
    assert p2.removeprefix("tether.") in Repo.find(vcs_root).backend_for(
        "memory"
    ).list_pins({"system": system})
    r = runner.invoke(app, ["gc", "--no-dry-run", "--json"])
    assert (
        r.exit_code == 0
        and p2.removeprefix("tether.") in json.loads(r.output)["unpinned"]["memory"]
    )
    r = runner.invoke(app, ["ops"])
    assert "abandon" in r.output.splitlines()[1]


def test_cli_restore(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    s1 = store.system(system).branches["main"]
    r = runner.invoke(app, ["commit", "-m", "v1", "--json"])
    c1 = json.loads(r.output)["vcs_commit"]
    store.write(system, "main", {"v": 2})
    assert runner.invoke(app, ["commit", "-m", "v2"]).exit_code == 0
    r = runner.invoke(app, ["new", "-b", "work", "--eager", "--json"])
    wref = json.loads(r.output)["working_refs"]["db"]
    assert store.resolve(system, wref) != s1
    r = runner.invoke(app, ["restore", "db", "--from", c1, "--dry-run"])
    assert r.exit_code == 0 and "fork" in r.output and wref in r.output
    r = runner.invoke(app, ["restore", "db", "--from", c1])
    assert r.exit_code == 0, r.output
    assert f"db -> {wref}" in r.output and store.resolve(system, wref) == s1
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.output)["stale"] is False


def test_cli_forget_workspace(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "v1"]).exit_code == 0
    r = runner.invoke(app, ["new", "-b", "work", "--eager", "--json"])
    wref = json.loads(r.output)["working_refs"]["db"]
    ws8 = Repo.find(vcs_root).workspace.workspace_id[:8]
    r = runner.invoke(app, ["forget-workspace", "--dry-run"])
    assert r.exit_code == 0 and "delete-file" in r.output
    assert "delete-branch" not in r.output  # branches belong to the bookmark
    r = runner.invoke(app, ["forget-workspace", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["workspace"] == ws8 and "deleted_working_refs" not in payload
    assert any(p.endswith("workspace.toml") for p in payload["removed_files"])
    assert wref in store.system(system).branches  # until the bookmark goes and gc runs


def test_cli_status_and_ops_flag_vcs_drift(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "v1"]).exit_code == 0
    store.write(system, "main", {"v": 2})
    r = runner.invoke(app, ["commit", "-m", "v2", "--json"])
    c2 = json.loads(r.output)["vcs_commit"]
    kind = Repo.find(vcs_root).vcs.kind
    if kind == "jj":
        subprocess.run(
            ["jj", "abandon", c2], cwd=vcs_root, check=True, capture_output=True
        )
    else:
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=vcs_root,
            check=True,
            capture_output=True,
        )
    r = runner.invoke(app, ["status", "--no-snapshot"])
    assert r.exit_code == 0, r.output
    assert (
        "warning:" in r.output and c2[:12] in r.output and "outside tether" in r.output
    )
    r = runner.invoke(app, ["status", "--no-snapshot", "--json"])
    assert json.loads(r.output)["vcs_drift"][0]["commit"] == c2
    r = runner.invoke(app, ["ops"])
    line = next(ln for ln in r.output.splitlines() if "v2" in ln or c2[:12] in ln)
    assert line.endswith("(vcs commit gone)")


def test_cli_pull(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tether.repo import Repo

    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store = default_store()
    store.system(system)
    store.write(system, "main", {"v": 1})
    Repo.init(vcs_root)
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["commit", "-m", "baseline"]).exit_code == 0

    # On the trunk main *is* the working ref: a fan-out shows it modified and
    # `pull` commits the head onto main.
    store.write(system, "main", {"v": 2})
    r = runner.invoke(app, ["status", "--snapshot"])
    assert r.exit_code == 0 and "modified" in r.output
    r = runner.invoke(app, ["pull"])
    assert r.exit_code == 0, r.output
    assert "pulled db" in r.output and "committed" in r.output and "on main" in r.output
    r = runner.invoke(app, ["status", "--json"])
    assert json.loads(r.output)["objects"][0]["state"] == "clean"
    r = runner.invoke(app, ["pull", "--json"])
    payload = json.loads(r.output)
    assert payload["unchanged"] == ["db"] and payload["vcs_commit"] is None
    r = runner.invoke(app, ["pull"])
    assert r.exit_code == 0 and "main is up to date" in r.output

    # Off the trunk, a lazily forked object sits at its pin: nothing to fetch.
    assert runner.invoke(app, ["new", "-b", "work"]).exit_code == 0
    store.write(system, "main", {"v": 3})
    r = runner.invoke(app, ["pull"])
    assert r.exit_code == 0 and "skipped db: no branch yet" in r.output
    r = runner.invoke(app, ["pull", "main"])
    assert r.exit_code != 0 and "tether new main" in r.output


def test_cli_set(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tether.repo import Repo

    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    repo = Repo.init(vcs_root)
    repo.add("db", "memory", {"system": system, "branch": "main"})
    repo.add("db2", "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    repo.new(eager=True)

    r = runner.invoke(app, ["set", "db", "--pin", "record"])
    assert r.exit_code == 0, r.output
    assert "set db  pin native -> record" in r.output
    r = runner.invoke(app, ["set", "--all", "--pin", "record", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert set(payload["changed"]) == {"db2"} and payload["unchanged"] == ["db"]
    r = runner.invoke(app, ["set", "db"])
    assert r.exit_code != 0 and "nothing to set" in r.output
    r = runner.invoke(app, ["set", "--pin", "native"])
    assert r.exit_code != 0 and "--all" in r.output


def test_cli_version_and_backends() -> None:
    from tether import __version__

    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and r.output.strip() == f"tether {__version__}"
    r = runner.invoke(app, ["backends"])
    assert r.exit_code == 0
    lines = {line.split()[0]: line for line in r.output.splitlines()}
    assert "stable" in lines["memory"] and "forkable" in lines["memory"]
    assert "experimental" in lines["neon"]
    r = runner.invoke(app, ["backends", "--json"])
    rows = {row["kind"]: row for row in json.loads(r.output)}
    assert (
        rows["file"]["maturity"] == "stable" and "diff" in rows["file"]["capabilities"]
    )
    assert rows["dolt"]["maturity"] == "experimental"


def test_experimental_is_an_import_boundary() -> None:
    """`import tether` (and `tether.cli`) load nothing under
    `tether.experimental.registry`; the public names are still importable
    from `tether` (resolved on first access), kinds resolve to the new
    modules, and the alpha-era module paths are gone."""
    import importlib
    import subprocess
    import sys

    import tether
    from tether.backends.base import build_backend, known_kinds

    # A fresh interpreter: what does `import tether; import tether.cli` load?
    code = (
        "import sys, tether, tether.cli; "
        "print(sorted(m for m in sys.modules if m.startswith('tether.experimental')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert "tether.experimental.registry" not in out, out
    assert "tether.experimental.backends" not in out, out
    assert "tether.experimental.lifecycle" not in out, out
    # ...and neither does a stable lifecycle of a memory object: init, add,
    # commit (a pin), new, open (a fork), commit. The touched journal those
    # write is core-owned; only `gc --delete-stores` reads it.
    code = (
        "import sys, subprocess, tempfile, pathlib\n"
        "root = pathlib.Path(tempfile.mkdtemp())\n"
        "subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)\n"
        "subprocess.run(['git', '-C', str(root), 'commit', '-q', '--allow-empty', "
        "'-m', 'root'], check=True, env={'GIT_AUTHOR_NAME': 'x', 'GIT_AUTHOR_EMAIL': "
        "'x@y', 'GIT_COMMITTER_NAME': 'x', 'GIT_COMMITTER_EMAIL': 'x@y', "
        "'PATH': __import__('os').environ['PATH']})\n"
        "from tether import Repo\n"
        "from tether.backends.memory import default_store\n"
        "default_store().system('s')\n"
        "repo = Repo.init(root)\n"
        "repo.add('db', 'memory', {'system': 's'})\n"
        "repo.commit('v1')\n"
        "repo.new(bookmark='w')\n"
        "repo.open('db').write({'x': 1})\n"
        "repo.commit('v2')\n"
        "print(sorted(m for m in sys.modules if m.startswith('tether.experimental')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert "tether.experimental.lifecycle" not in out, out

    assert {"neon", "dolt", "ducklake", "iceberg"} <= set(known_kinds())
    assert type(build_backend("neon", {})).__module__ == (
        "tether.experimental.backends.neon"
    )
    assert tether.ExportBundle.__module__ == "tether.experimental.registry.export"
    assert tether.ImportSpec.__module__ == "tether.experimental.registry.registry"
    assert tether.ImportReport.__module__ == "tether.experimental.registry.ops"
    assert "ExportBundle" in dir(tether) and "ExportBundle" in tether.__all__
    with pytest.raises(AttributeError):
        tether.NoSuchName  # noqa: B018
    for old in (
        "tether.backends.neon",
        "tether.backends.iceberg",
        "tether.export",
        "tether.registry",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old)


def test_open_redacts_the_neon_password_by_default(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell history and CI logs keep stdout: `tether open` prints a
    connection URL with its password redacted unless --with-password, and
    never with it under --json."""
    from tether.handles import NeonHandle
    from tether.repo import Repo

    monkeypatch.chdir(vcs_root)
    assert runner.invoke(app, ["init"]).exit_code == 0
    full = "postgresql://runner:s3cret@ep.neon.tech/neondb"
    monkeypatch.setattr(
        Repo,
        "open",
        lambda self, key, **kw: NeonHandle(
            key="main", read_only=True, url=full, branch="main"
        ),
    )
    r = runner.invoke(app, ["open", "db"])
    assert r.exit_code == 0 and "s3cret" not in r.output and "***" in r.output
    r = runner.invoke(app, ["open", "db", "--with-password"])
    assert r.exit_code == 0 and full in r.output
    r = runner.invoke(app, ["open", "db", "--json", "--with-password"])
    assert r.exit_code == 0 and "s3cret" not in r.output
    assert (
        json.loads(r.output)["address"] == "postgresql://runner:***@ep.neon.tech/neondb"
    )


def test_validate_locator_refuses_cheap_mistakes_at_add(vcs_root: Path) -> None:
    from tether.errors import BackendError
    from tether.repo import Repo

    repo = Repo.init(vcs_root)
    with pytest.raises(BackendError, match="unsupported icechunk storage scheme"):
        repo.add("z", "icechunk", {"uri": "gs://bucket/repo"})
    with pytest.raises(BackendError, match="delta `at` must be"):
        repo.add("d", "delta", {"uri": str(vcs_root / "t"), "at": "v3"})
    with pytest.raises(BackendError, match="ducklake `at` must be"):
        repo.add("l", "ducklake", {"metadata": str(vcs_root / "m.ducklake"), "at": "x"})
    assert repo.objects == {}


@pytest.mark.parametrize(
    "command",
    [
        ["commit"],
        ["new"],
        ["repair"],
        ["restore", "db", "--from", "@"],
        ["forget-workspace"],
        ["drop", "probe"],
        ["upgrade"],
        ["gc"],
        ["promote"],
        ["import", "reg.csv"],
    ],
)
@pytest.mark.parametrize("preview", [["--dry-run"], ["--plan", "out.json"]])
def test_from_plan_refuses_a_preview_flag(
    command: list[str],
    preview: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--from-plan` applies; `--dry-run` and `--plan` preview. Together the
    apply used to win silently: `commit --from-plan p --dry-run` committed."""
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, [*command, "--from-plan", "p.json", *preview])
    assert r.exit_code == 1, r.output
    assert "cannot be combined" in r.output
    assert not (tmp_path / "out.json").exists()


def test_commit_from_plan_with_dry_run_commits_nothing(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    system = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(system)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(
        app, ["add", "db", "--kind", "memory", "--set", f"system={system}"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["commit", "-m", "v1", "--plan", "p.json"])
    assert r.exit_code == 0, r.output
    head = Repo.find(vcs_root)._vcs_head_or_none()
    r = runner.invoke(app, ["commit", "--from-plan", "p.json", "--dry-run"])
    assert r.exit_code == 1 and "cannot be combined" in r.output, r.output
    repo = Repo.find(vcs_root)
    assert repo._vcs_head_or_none() == head and repo.objects["db"].state is None


def test_status_labels_an_unreadable_object_and_exits_1(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(vcs_root)
    repo = Repo.init(vcs_root)
    systems = {key: f"sys-{uuid.uuid4().hex[:8]}" for key in ("bad", "ok")}
    for key, system in systems.items():
        default_store().system(system)
        repo.add(key, "memory", {"system": system, "branch": "main"})
    repo.commit("baseline")
    default_store().deleted.add(systems["bad"])
    try:
        r = runner.invoke(app, ["status", "--snapshot", "--json"])
        assert r.exit_code == 1, r.output
        objects = {o["key"]: o for o in json.loads(r.stdout)["objects"]}
        assert objects["bad"]["state"] == "error"
        assert "deleted" in objects["bad"]["error"]
        assert (objects["ok"]["state"], objects["ok"]["error"]) == ("clean", None)
        r = runner.invoke(app, ["status", "--snapshot"])
        assert r.exit_code == 1
        assert "error  bad" in r.stdout and "clean  ok" in r.stdout
        assert "bad:" in r.stderr and "deleted" in r.stderr
    finally:
        default_store().deleted.discard(systems["bad"])
