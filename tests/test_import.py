from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from tether.backends.memory import default_store
from tether.errors import ConfigError, StalePlanError
from tether.manifest import Policy
from tether.plan import Plan
from tether.registry import CANONICAL_COLUMNS, ImportSpec, read_source, specs_from_rows
from tether.repo import Repo


def _system() -> str:
    name = f"sys-{uuid.uuid4().hex[:8]}"
    default_store().system(name)
    return name


def test_import_spec_validation() -> None:
    defaults = Policy()
    spec = ImportSpec.from_row(
        {"key": "zarr/a", "kind": "memory", "uri": "mem://a", "policy_pin": "record"},
        defaults,
    )
    assert spec.locator == {"uri": "mem://a"} and spec.policy.pin == "record"
    assert spec.policy.write == "fork"  # default filled in

    # locator_json wins over uri when both name `uri`; `at` lands in the locator.
    spec = ImportSpec.from_row(
        {
            "key": "db",
            "kind": "memory",
            "uri": "ignored",
            "locator_json": json.dumps({"system": "s1", "branch": "main", "uri": "x"}),
            "at": "snap-9",
        },
        defaults,
    )
    assert spec.locator == {
        "system": "s1",
        "branch": "main",
        "uri": "x",
        "at": "snap-9",
    }

    for bad in (
        {"kind": "memory", "uri": "u"},  # no key
        {"key": "k", "uri": "u"},  # no kind
        {"key": "k", "kind": "nope", "uri": "u"},  # unknown kind
        {"key": "../k", "kind": "memory", "uri": "u"},  # unsafe key
        {"key": "k", "kind": "memory"},  # no locator
        {"key": "k", "kind": "memory", "locator_json": "[1]"},  # not an object
        {"key": "k", "kind": "memory", "uri": "u", "policy_write": "maybe"},
    ):
        with pytest.raises(ConfigError):
            ImportSpec.from_row(bad, defaults)

    specs, notes = specs_from_rows(
        [{"key": "a", "kind": "memory", "uri": "u", "owner": "x", "created": 1}],
        defaults,
    )
    assert [s.key for s in specs] == ["a"] and notes == [
        "ignored columns: created, owner"
    ]
    with pytest.raises(ConfigError):
        specs_from_rows(
            [{"key": "a", "kind": "memory", "uri": "u"}] * 2, defaults
        )  # duplicate key


def test_read_source_csv_jsonl_sqlite(tmp_path: Path) -> None:
    rows = [
        {"key": "db", "kind": "memory", "uri": "", "locator_json": '{"system": "s"}'},
        {"key": "f", "kind": "file", "uri": "s3://b/k", "locator_json": ""},
    ]
    csv_path = tmp_path / "reg.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key", "kind", "uri", "locator_json"])
        w.writeheader()
        w.writerows(rows)
    assert [r["key"] for r in read_source(str(csv_path))] == ["db", "f"]

    jsonl = tmp_path / "reg.jsonl"
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert read_source(str(jsonl))[1]["uri"] == "s3://b/k"

    db = tmp_path / "reg.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE data_object (name TEXT, object_uri TEXT, type TEXT)")
    con.executemany(
        "INSERT INTO data_object VALUES (?, ?, ?)",
        [
            ("plate1", "s3://lab/plate1", "IcechunkStore"),
            ("notes", "s3://lab/n", "File"),
        ],
    )
    con.commit()
    con.close()
    got = read_source(
        str(db),
        query=(
            "SELECT 'zarr/' || name AS key, 'icechunk' AS kind, object_uri AS uri "
            "FROM data_object WHERE type = 'IcechunkStore'"
        ),
    )
    assert got == [{"key": "zarr/plate1", "kind": "icechunk", "uri": "s3://lab/plate1"}]
    assert len(read_source(str(db), table="data_object")) == 2
    with pytest.raises(ConfigError):
        read_source(str(db))  # neither table nor query
    with pytest.raises(ConfigError):
        read_source(str(db), table="data_object", query="SELECT 1")
    with pytest.raises(ConfigError):
        read_source(str(tmp_path / "missing.sqlite"), table="t")


def test_plan_and_apply_import(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    s1, s2 = _system(), _system()
    rows = [
        {
            "key": "db/one",
            "kind": "memory",
            "locator_json": {"system": s1, "branch": "main"},
        },
        {
            "key": "db/two",
            "kind": "memory",
            "locator_json": {"system": s2, "branch": "main"},
            "policy_pin": "record",
        },
    ]
    specs, _ = specs_from_rows(rows, repo.config.defaults)
    plan = repo.plan_import(specs)
    assert plan.command == "import"
    assert [(a.op, a.key) for a in plan.actions] == [
        ("add", "db/one"),
        ("add", "db/two"),
    ]

    # Plans round-trip through JSON and apply through the same path as the CLI.
    report = repo.apply_import(Plan.from_json(plan.to_json()))
    assert report.added == ["db/one", "db/two"] and not report.updated
    assert repo.objects["db/two"].policy.pin == "record"
    assert repo.objects["db/one"].state is None  # import never records state

    # Re-importing the same rows is a no-op; a policy change is an update that
    # keeps the committed state; a missing key is removed only with sync.
    repo.commit("baseline")
    committed = repo.objects["db/one"].state
    assert committed is not None
    rows[0]["policy_write"] = "track"
    specs, _ = specs_from_rows(rows[:1], repo.config.defaults)
    plan = repo.plan_import(specs)
    assert [(a.op, a.key) for a in plan.actions] == [("update", "db/one")]
    assert "policy changed" in plan.actions[0].detail
    plan = repo.plan_import(specs, sync=True)
    assert [(a.op, a.key) for a in plan.actions] == [
        ("update", "db/one"),
        ("remove", "db/two"),
    ]
    report = repo.apply_import(plan)
    assert report.updated == ["db/one"] and report.removed == ["db/two"]
    assert repo.objects["db/one"].policy.write == "track"
    assert repo.objects["db/one"].state == committed  # state kept
    assert "db/two" not in repo.objects
    assert not repo.is_stale()  # baseline refreshed

    # Identical rows -> empty plan with an "unchanged" note.
    plan = repo.plan_import(specs)
    assert plan.is_empty and any("db/one: unchanged" in n for n in plan.notes)

    # A kind change is refused rather than silently rewriting the manifest.
    specs, _ = specs_from_rows(
        [{"key": "db/one", "kind": "file", "uri": "s3://x"}], repo.config.defaults
    )
    with pytest.raises(ConfigError):
        repo.plan_import(specs)

    # A stale plan (manifests changed after planning) is refused.
    specs, _ = specs_from_rows(
        [{"key": "db/three", "kind": "memory", "locator_json": {"system": _system()}}],
        repo.config.defaults,
    )
    plan = repo.plan_import(specs)
    repo.add("other", "memory", {"system": _system()})
    with pytest.raises(StalePlanError):
        repo.apply_import(plan)
    with pytest.raises(ConfigError):
        repo.apply_commit(plan)  # wrong plan kind


def test_import_at_column_and_convenience(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    system = _system()
    store = default_store()
    s1 = store.write(system, "main", {"v": 1})
    store.write(system, "main", {"v": 2})
    report = repo.import_objects(
        [{"key": "db", "kind": "memory", "locator_json": {"system": system}, "at": s1}]
    )
    assert report.added == ["db"]
    assert repo.objects["db"].locator["at"] == s1
    assert repo.snapshot()["db"] == {"snapshot_id": s1}  # detached at the older state


def test_export_import_round_trip(vcs_root: Path, tmp_path: Path) -> None:
    """Exporting the head and importing it back plans nothing."""
    repo = Repo.init(vcs_root)
    repo.add("db", "memory", {"system": _system(), "branch": "main"})
    repo.add(
        "notes",
        "file",
        {"uri": str(tmp_path / "notes")},
        policy=Policy(file="versioned"),
    )
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.txt").write_text("a")
    repo.commit("baseline")

    db = tmp_path / "export.sqlite"
    repo.export().to_sqlite(db)
    rows = read_source(
        str(db),
        query=(
            "SELECT key, kind, locator_json, policy_write, policy_file, policy_pin "
            "FROM objects_head"
        ),
    )
    assert {r["key"] for r in rows} == {"db", "notes"}
    specs, notes = specs_from_rows(rows, repo.config.defaults)
    assert notes == []
    plan = repo.plan_import(specs, sync=True)
    assert plan.is_empty, plan.render()
    assert set(CANONICAL_COLUMNS) >= set(rows[0])
