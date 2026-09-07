from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from tether.backends.memory import default_store
from tether.errors import ConfigError
from tether.export import (
    SCHEMA_VERSION,
    TABLES,
    ExportBundle,
    postgres_ddl,
    postgres_upsert_sql,
    sqlite_ddl,
)
from tether.manifest import Policy
from tether.repo import Repo


def _pg(dsn: str) -> Any:
    """A psycopg connection, untyped: tests run dynamic SQL ty cannot check."""
    import psycopg

    return psycopg.connect(dsn)


def _dataset(vcs_root: Path) -> tuple[Repo, str, list[str]]:
    """A repo with a memory object and a versioned directory, committed three times."""
    repo = Repo.init(vcs_root)
    store = default_store()
    system = f"sys-{uuid.uuid4().hex[:8]}"
    store.system(system)
    store.write(system, "main", {"v": 1})
    repo.add("db", "memory", {"system": system, "branch": "main"})
    data = vcs_root / "plate"
    data.mkdir()
    (data / "a.bin").write_bytes(b"aaaa")
    repo.add("raw/plate", "file", {"uri": str(data)}, policy=Policy(file="versioned"))
    c1 = repo.commit("baseline").vcs_commit
    store.write(system, "main", {"v": 2})
    c2 = repo.commit("db moved").vcs_commit  # plate unchanged
    (data / "b.bin").write_bytes(b"bb")
    c3 = repo.commit("plate moved").vcs_commit  # db unchanged
    assert c1 and c2 and c3
    return repo, system, [c1, c2, c3]


def _extra(repo: Repo) -> int:
    """jj keeps an empty working-copy commit whose tree still holds the manifests."""
    return 2 if repo.vcs.kind == "jj" else 0


def _rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_export_all_history_to_sqlite(vcs_root: Path, tmp_path: Path) -> None:
    repo, _system, commits = _dataset(vcs_root)
    bundle = repo.export()
    out = bundle.to_sqlite(tmp_path / "tether.sqlite")

    commit_ids = {r[0] for r in _rows(out, "SELECT commit_id FROM commits")}
    assert set(commits) <= commit_ids
    # Every tether commit carries both objects; earlier VCS commits carry none.
    assert _rows(out, "SELECT COUNT(*) FROM objects")[0][0] == 6 + _extra(repo)
    per_commit = dict(
        _rows(out, "SELECT commit_id, COUNT(*) FROM objects GROUP BY commit_id")
    )
    assert all(per_commit[c] == 2 for c in commits)

    # Distinct states: db has 2, plate has 2 -> 4 rows, keyed by first commit.
    states = _rows(
        out,
        "SELECT kind, first_commit_id, pin_id IS NOT NULL FROM object_states "
        "ORDER BY kind, first_committed_at",
    )
    assert len(states) == 4
    assert [s[0] for s in states] == ["file", "file", "memory", "memory"]
    assert {s[1] for s in states if s[0] == "memory"} == {commits[0], commits[1]}
    assert all(s[2] == 1 for s in states if s[0] == "memory")  # pinned
    assert all(s[2] == 0 for s in states if s[0] == "file")  # addressable, no pin

    # Refs include the head; the objects_head view sees the working tree's objects.
    refs = dict(_rows(out, "SELECT kind, commit_id FROM refs WHERE kind = 'head'"))
    assert refs["head"] == repo.vcs.current_rev()
    head_objects = _rows(out, "SELECT key, uri FROM objects_head ORDER BY key")
    assert [k for k, _ in head_objects] == ["db", "raw/plate"]
    pins = _rows(out, "SELECT kind, commits FROM object_pins ORDER BY kind, commits")
    # v1 is pinned at c1; v2 at c2 and c3 (and jj's working-copy commit).
    assert pins == [("memory", 1), ("memory", 2 + _extra(repo) // 2)]

    # Parent edges and metadata.
    parents = dict(_rows(out, "SELECT commit_id, parent_id FROM commit_parents"))
    assert parents[commits[1]] == commits[0] and parents[commits[2]] == commits[1]
    meta = dict(_rows(out, "SELECT key, value FROM tether_meta"))
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert meta["vcs"] == repo.vcs.kind and meta["head"] == repo.vcs.current_rev()
    if repo.vcs.kind == "jj":
        change_ids = _rows(out, "SELECT change_id FROM commits WHERE change_id IS NULL")
        assert change_ids == []

    # JSON columns hold real JSON; identity and state hashes are populated.
    row = _rows(
        out,
        "SELECT locator_json, state_json, identity_hash, state_hash FROM objects "
        f"WHERE key = 'db' AND commit_id = '{commits[0]}'",
    )[0]
    assert json.loads(row[0])["branch"] == "main"
    assert json.loads(row[1])["snapshot_id"]
    assert len(row[2]) == 32 and len(row[3]) == 32


def test_export_rev_subset_listings_and_workspace(
    vcs_root: Path, tmp_path: Path
) -> None:
    repo, _system, commits = _dataset(vcs_root)
    repo.new(eager=True)
    bundle = repo.export([commits[0]], listings=True, workspace=True)
    assert bundle.row_counts()["commits"] == 1
    assert bundle.row_counts()["objects"] == 2
    # Refs pointing outside the exported commits are dropped (no dangling FKs).
    assert all(r["commit_id"] == commits[0] for r in bundle["refs"].rows)
    # The directory listing for plate@c1 has one file.
    assert bundle.row_counts()["listings"] == 1
    entries = bundle["listing_entries"].rows
    assert [e["path"] for e in entries] == ["a.bin"] and entries[0]["size"] == 4
    ws = {r["key"]: r for r in bundle["workspace"].rows}
    assert ws["db"]["working_ref"].startswith("tether.ws.")
    assert ws["raw/plate"]["working_ref"] is None  # not forkable


def test_export_directory_formats(vcs_root: Path, tmp_path: Path) -> None:
    repo, _system, _commits = _dataset(vcs_root)
    bundle = repo.export()

    files = bundle.to_dir(tmp_path / "jsonl", "jsonl")
    names = {p.name for p in files}
    assert "schema.json" in names and "objects.jsonl" in names
    schema = json.loads((tmp_path / "jsonl" / "schema.json").read_text())
    assert set(schema["tables"]) == set(TABLES) and "objects_head" in schema["views"]
    objects = [
        json.loads(line)
        for line in (tmp_path / "jsonl" / "objects.jsonl").read_text().splitlines()
    ]
    assert len(objects) == 6 + _extra(repo)
    assert isinstance(objects[0]["locator_json"], dict)

    bundle.to_dir(tmp_path / "csv", "csv")
    with (tmp_path / "csv" / "objects.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6 + _extra(repo)
    assert json.loads(rows[0]["locator_json"])  # JSON as text in CSV
    assert rows[0]["recoverable"] in ("True", "False")

    with pytest.raises(ConfigError):
        bundle.to_dir(tmp_path / "x", "xlsx")


def test_export_parquet_and_arrow(vcs_root: Path, tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    repo, _system, _commits = _dataset(vcs_root)
    bundle = repo.export()
    bundle.to_dir(tmp_path / "pq", "parquet")
    table = pq.read_table(tmp_path / "pq" / "objects.parquet")
    assert table.num_rows == 6 + _extra(repo)
    assert set(table.column_names) == set(TABLES["objects"].column_names)
    arrow = bundle.to_arrow()
    assert arrow["commits"].num_rows == bundle.row_counts()["commits"]


def test_export_append_is_idempotent_and_incremental(
    vcs_root: Path, tmp_path: Path
) -> None:
    repo, system, commits = _dataset(vcs_root)
    out = tmp_path / "t.sqlite"
    repo.export().to_sqlite(out)
    before = _rows(out, "SELECT COUNT(*) FROM objects")[0][0]
    repo.export().to_sqlite(out, append=True)
    assert _rows(out, "SELECT COUNT(*) FROM objects")[0][0] == before
    assert _rows(out, "SELECT COUNT(*) FROM refs WHERE kind = 'head'")[0][0] == 1

    default_store().write(system, "main", {"v": 3})
    c4 = repo.commit("again").vcs_commit
    repo.export().to_sqlite(out, append=True)
    # git: c4's two manifests. jj: c4 plus the fresh working-copy commit; the
    # rewritten old working-copy row lingers (append never deletes).
    assert _rows(out, "SELECT COUNT(*) FROM objects")[0][0] == before + 2 + _extra(repo)
    assert (c4,) in _rows(out, "SELECT commit_id FROM commits")
    # Without append the file is replaced, not merged.
    repo.export([commits[0]]).to_sqlite(out)
    assert _rows(out, "SELECT COUNT(*) FROM commits")[0][0] == 1


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def test_postgres_statements() -> None:
    ddl = postgres_ddl("tether")
    assert ddl[0] == "CREATE SCHEMA IF NOT EXISTS tether"
    objects = next(
        s for s in ddl if s.startswith("CREATE TABLE IF NOT EXISTS tether.objects")
    )
    assert "locator_json JSONB" in objects and "captured_at TIMESTAMPTZ" in objects
    assert (
        "recoverable BOOLEAN" in objects and "PRIMARY KEY (commit_id, key)" in objects
    )
    assert any(s.startswith("CREATE OR REPLACE VIEW tether.objects_head") for s in ddl)

    up = postgres_upsert_sql("commits", "tether")
    assert up.startswith("INSERT INTO tether.commits (commit_id, change_id")
    assert "%s::timestamptz" in up and "ON CONFLICT (commit_id) DO UPDATE SET" in up
    assert "message = EXCLUDED.message" in up
    parents = postgres_upsert_sql("commit_parents")
    assert parents.endswith(
        "ON CONFLICT (commit_id, parent_id) DO UPDATE SET position = EXCLUDED.position"
    )
    assert "%s::jsonb" in postgres_upsert_sql("objects")

    lite = sqlite_ddl()
    assert any("CREATE VIEW IF NOT EXISTS object_pins" in s for s in lite)
    with pytest.raises(ConfigError):
        postgres_ddl("bad-name")


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.conn.executed.append(sql)
        if sql.startswith("SELECT commit_id FROM"):
            if not self.conn.has_schema:
                raise RuntimeError('relation "tether.commits" does not exist')
            self._result = [(c,) for c in self.conn.existing]
        elif sql.startswith("CREATE SCHEMA"):
            self.conn.has_schema = True

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.conn.executed.append(sql)
        table = sql.split("INSERT INTO ")[1].split(" ")[0]
        self.conn.inserted.setdefault(table, []).extend(rows)

    def fetchall(self) -> list[tuple]:
        return self._result


class _FakeConnection:
    def __init__(self, existing: set[str], has_schema: bool = True) -> None:
        self.existing = existing
        self.has_schema = has_schema
        self.executed: list[str] = []
        self.inserted: dict[str, list[tuple]] = {}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_publish_skips_known_commits_and_refreshes_refs(vcs_root: Path) -> None:
    repo, _system, commits = _dataset(vcs_root)
    bundle = repo.export()

    fake = _FakeConnection(existing={commits[0], commits[1]})
    report = bundle.to_postgres(connection=fake, schema="tether")
    assert report.skipped_commits == 2
    # Only the third tether commit (and any non-tether commits) are inserted.
    inserted_commits = {r[0] for r in fake.inserted["tether.commits"]}
    assert commits[2] in inserted_commits and commits[0] not in inserted_commits
    assert report.upserted["objects"] == 2 + _extra(
        repo
    )  # manifests at commits[2] (+ jj @)
    assert "DELETE FROM tether.refs" in fake.executed
    assert "DELETE FROM tether.tether_meta" in fake.executed
    assert fake.executed[0] == "CREATE SCHEMA IF NOT EXISTS tether"
    assert fake.commits == 1 and fake.rollbacks == 0
    assert not fake.closed  # caller-owned connections stay open
    # JSON travels as text; booleans as bool; the server casts.
    obj_row = fake.inserted["tether.objects"][0]
    cols = TABLES["objects"].column_names
    assert json.loads(obj_row[cols.index("locator_json")])
    assert isinstance(obj_row[cols.index("recoverable")], bool)


def test_publish_dry_run_writes_nothing(vcs_root: Path) -> None:
    repo, _system, _commits = _dataset(vcs_root)
    bundle = repo.export()
    fake = _FakeConnection(existing=set(), has_schema=False)
    report = bundle.to_postgres(connection=fake, dry_run=True)
    assert report.dry_run and report.skipped_commits == 0
    assert report.upserted["objects"] == 6 + _extra(repo)
    assert not fake.inserted
    assert not any(s.startswith("CREATE") for s in fake.executed)
    assert fake.commits == 0 and fake.rollbacks >= 1


def test_publish_live(vcs_root: Path, pg_dsn: str) -> None:
    """Round trip against a real Postgres: DDL, upserts, casts, incremental runs."""
    repo, system, commits = _dataset(vcs_root)

    first = repo.export().to_postgres(pg_dsn, schema="tether")
    assert first.skipped_commits == 0 and first.upserted["objects"] == 6 + _extra(repo)

    with _pg(pg_dsn) as conn:

        def one(sql: str) -> object:
            return conn.execute(sql).fetchone()[0]

        assert one("SELECT COUNT(*) FROM tether.objects") == 6 + _extra(repo)
        assert one("SELECT COUNT(*) FROM tether.objects_head") == 2
        # JSONB and TIMESTAMPTZ really are typed on the server side.
        assert one(
            "SELECT state_json->>'snapshot_id' FROM tether.objects "
            f"WHERE key = 'db' AND commit_id = '{commits[0]}'"
        )
        assert (
            one("SELECT pg_typeof(committed_at)::text FROM tether.commits LIMIT 1")
            == "timestamp with time zone"
        )
        assert one(
            "SELECT pg_typeof(locator_json)::text FROM tether.objects LIMIT 1"
        ) == ("jsonb")
        assert (
            one("SELECT recoverable FROM tether.objects WHERE key = 'db' LIMIT 1")
            is True
        )
        assert one("SELECT value FROM tether.tether_meta WHERE key = 'head'") == (
            repo.vcs.current_rev()
        )
        # The dedup table and the pins view line up with the SQLite export.
        assert one("SELECT COUNT(*) FROM tether.object_states") == 4
        assert (
            one("SELECT MAX(commits) FROM tether.object_pins") == 2 + _extra(repo) // 2
        )

    # Publishing again writes no commit rows but refreshes refs and meta.
    second = repo.export().to_postgres(pg_dsn, schema="tether")
    assert second.skipped_commits == first.upserted["commits"]
    assert second.upserted["commits"] == 0 and second.upserted["objects"] == 0
    assert second.upserted["refs"] >= 1

    # A new commit publishes incrementally; the head ref moves with it.
    default_store().write(system, "main", {"v": 3})
    c4 = repo.commit("again").vcs_commit
    third = repo.export().to_postgres(pg_dsn, schema="tether")
    assert third.upserted["commits"] == 1 + _extra(repo) // 2  # c4 (+ jj's new @)
    with _pg(pg_dsn) as conn:
        head = conn.execute("SELECT commit_id FROM tether.refs WHERE kind = 'head'")
        assert head.fetchone()[0] == repo.vcs.current_rev()
        n = conn.execute(
            "SELECT COUNT(*) FROM tether.objects WHERE commit_id = %s", (c4,)
        )
        assert n.fetchone()[0] == 2

    # A caller-owned connection is reused and left open; dry_run writes nothing.
    with _pg(pg_dsn) as conn:
        report = repo.export().to_postgres(
            connection=conn, schema="tether", dry_run=True
        )
        assert report.dry_run and report.upserted["commits"] == 0
        assert not conn.closed
        # A fresh schema holds exactly the reachable history. `tether` (published
        # incrementally) may hold one more: jj rewrote the old working-copy
        # commit at `commit("again")`, and publish never deletes commit rows.
        other = repo.export().to_postgres(connection=conn, schema="tether_two")
        reachable = repo.export().row_counts()["commits"]
        assert other.upserted["commits"] == reachable
        n = conn.execute("SELECT COUNT(*) FROM tether_two.commits").fetchone()[0]
        assert n == reachable
        n = conn.execute("SELECT COUNT(*) FROM tether.commits").fetchone()[0]
        assert n == reachable + _extra(repo) // 2


def test_bundle_row_counts_and_getitem(vcs_root: Path) -> None:
    repo, _system, _commits = _dataset(vcs_root)
    bundle = repo.export()
    assert isinstance(bundle, ExportBundle)
    assert bundle["objects"].name == "objects"
    assert bundle.row_counts()["listings"] == 0  # opt-in
