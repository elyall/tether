"""Dolt backend tests.

The backend's SQL surface is isolated behind ``DoltClient``; the conformance
suite runs against an in-memory fake, and ``SqlDoltClient`` is checked for the
exact statements it issues via a recording connection.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.backends.dolt import DoltBackend, SqlDoltClient
from tether.errors import BackendError
from tether.handles import DoltHandle
from tether.manifest import Locator, compute_pin_id, ref_for_pin
from tether.testing import run_conformance


# --------------------------------------------------------------------------- #
# In-memory fake of one Dolt database
# --------------------------------------------------------------------------- #
@dataclass
class FakeDoltDb:
    # commit -> {table: row count}; enough to fake dolt_diff_summary/stat.
    commits: dict[str, dict[str, int]] = field(default_factory=dict)
    parents: dict[str, str | None] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)  # name -> commit
    dirty: set[str] = field(default_factory=set)
    tags: dict[str, str] = field(default_factory=dict)

    def new_commit(
        self,
        seed: str,
        tables: dict[str, int] | None = None,
        parent: str | None = None,
        message: str = "",
    ) -> str:
        h = hashlib.sha1(seed.encode()).hexdigest()[:32]
        self.commits[h] = dict(tables or {})
        self.parents[h] = parent
        self.messages[h] = message
        return h

    def _lookup(self, ref: str) -> str:
        cid = self.branches.get(ref) or self.tags.get(ref) or ref
        if cid not in self.commits:
            raise RuntimeError(f"unknown ref {ref}")
        return cid

    # DoltClient protocol ------------------------------------------------- #
    def resolve(self, ref: str) -> str | None:
        try:
            return self._lookup(ref)
        except RuntimeError:
            return None

    def log(self, ref: str, limit: int) -> list[dict]:
        rows: list[dict] = []
        cursor: str | None = self.resolve(ref)
        while cursor is not None and len(rows) < limit:
            rows.append(
                {
                    "commit_hash": cursor,
                    "committer": "t",
                    "date": 1_700_000_000 + len(rows),
                    "message": self.messages.get(cursor, ""),
                }
            )
            cursor = self.parents.get(cursor)
        return rows

    def branch_head(self, branch: str) -> tuple[str, bool] | None:
        if branch not in self.branches:
            return None
        return self.branches[branch], branch in self.dirty

    def tag_hash(self, name: str) -> str | None:
        return self.tags.get(name)

    def list_tags(self) -> list[str]:
        return sorted(self.tags)

    def create_tag(self, name: str, ref: str) -> None:
        if name in self.tags:
            raise RuntimeError("tag exists")
        self.tags[name] = self._lookup(ref)

    def delete_tag(self, name: str) -> None:
        del self.tags[name]

    def commit_exists(self, commit: str) -> bool:
        return commit in self.commits

    def create_branch(self, name: str, ref: str) -> None:
        if name in self.branches:
            raise RuntimeError("branch exists")
        self.branches[name] = self._lookup(ref)

    def delete_branch(self, name: str) -> None:
        del self.branches[name]
        self.dirty.discard(name)

    def list_branches(self) -> list[str]:
        return sorted(self.branches)

    def merge(self, base: str, source: str, message: str, *, ff_only: bool) -> dict:
        head, src = self.branches[base], self._lookup(source)
        line = lambda c: [r["commit_hash"] for r in self.log(c, 10_000)]  # noqa: E731
        if src in line(head):
            return {
                "hash": head,
                "fast_forward": 0,
                "conflicts": 0,
                "conflict_tables": [],
            }
        if head in line(src):
            self.branches[base] = src
            return {
                "hash": src,
                "fast_forward": 1,
                "conflicts": 0,
                "conflict_tables": [],
            }
        if ff_only:
            raise RuntimeError("cannot fast-forward")
        common = next((c for c in line(head) if c in line(src)), None)
        anc = self.commits.get(common, {}) if common else {}
        ours, theirs = self.commits[head], self.commits[src]
        merged = dict(ours)
        conflicts = []
        for table in set(ours) | set(theirs) | set(anc):
            o, t, a = ours.get(table), theirs.get(table), anc.get(table)
            if o == t:
                continue
            if o == a:
                if t is None:
                    merged.pop(table, None)
                else:
                    merged[table] = t
            elif t != a:
                conflicts.append(table)
        if conflicts:
            return {
                "hash": head,
                "fast_forward": 0,
                "conflicts": len(conflicts),
                "conflict_tables": sorted(conflicts),
            }
        cid = self.new_commit(f"{head}+{src}", merged, parent=head, message=message)
        self.branches[base] = cid
        return {"hash": cid, "fast_forward": 0, "conflicts": 0, "conflict_tables": []}

    def diff_summary(self, from_ref: str, to_ref: str) -> list[dict]:
        ta, tb = (
            self.commits[self._lookup(from_ref)],
            self.commits[self._lookup(to_ref)],
        )
        rows = []
        for table in sorted(set(ta) | set(tb)):
            if table not in ta:
                diff_type = "added"
            elif table not in tb:
                diff_type = "dropped"
            elif ta[table] != tb[table]:
                diff_type = "modified"
            else:
                continue
            rows.append(
                {
                    "table_name": table,
                    "diff_type": diff_type,
                    "data_change": 1,
                    "schema_change": 0,
                }
            )
        return rows

    def diff_stat(self, from_ref: str, to_ref: str) -> list[dict]:
        ta, tb = (
            self.commits[self._lookup(from_ref)],
            self.commits[self._lookup(to_ref)],
        )
        rows = []
        for table in sorted(set(ta) | set(tb)):
            na, nb = ta.get(table, 0), tb.get(table, 0)
            if na == nb:
                continue
            rows.append(
                {
                    "table_name": table,
                    "rows_added": max(0, nb - na),
                    "rows_deleted": max(0, na - nb),
                    "rows_modified": 0,
                    "old_row_count": na,
                    "new_row_count": nb,
                }
            )
        return rows

    # test helpers -------------------------------------------------------- #
    def write(self, branch: str) -> None:
        self.dirty.add(branch)

    def commit(
        self, branch: str, msg: str, tables: dict[str, int] | None = None
    ) -> str:
        parent = self.branches[branch]
        base = dict(self.commits[parent])
        if tables is None:
            base["t"] = base.get("t", 0) + 1  # default mutation: one more row in t
        else:
            base.update(tables)
        cid = self.new_commit(f"{parent}:{msg}", base, parent=parent, message=msg)
        self.branches[branch] = cid
        self.dirty.discard(branch)
        return cid


class FakeDolt:
    def __init__(self) -> None:
        self.dbs: dict[str, FakeDoltDb] = {}

    def create_db(self) -> str:
        name = f"db_{uuid.uuid4().hex[:8]}"
        db = FakeDoltDb()
        db.branches["main"] = db.new_commit(name)
        self.dbs[name] = db
        return name

    def __call__(self, locator: Locator) -> FakeDoltDb:
        # Resolve `url` or `database` exactly as the backend does.
        return self.dbs[DoltBackend()._endpoint(locator)[2]]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> tuple[DoltBackend, FakeDolt]:
    b = DoltBackend()
    f = FakeDolt()
    monkeypatch.setattr(b, "_client", f)
    return b, f


class DoltHarness:
    def __init__(self, backend: DoltBackend, fake: FakeDolt) -> None:
        self.backend: ObjectBackend = backend
        self.fake = fake
        self._n = 0

    def new_object(self) -> Locator:
        return {
            "host": "dolt.test",
            "database": self.fake.create_db(),
            "branch": "main",
        }

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        db = self.fake(locator)
        branch = working_ref or str(locator.get("branch", "main"))
        db.write(branch)
        db.commit(branch, f"mutation {self._n}")


def test_dolt_conformance(fake: tuple[DoltBackend, FakeDolt]) -> None:
    b, f = fake
    assert Capability.FORK in b.capabilities
    run_conformance(DoltHarness(b, f))


def test_dolt_dirty_branch_and_handles(fake: tuple[DoltBackend, FakeDolt]) -> None:
    b, f = fake
    loc = {"url": f"mysql://alice@dolt.test:3307/{f.create_db()}", "branch": "main"}
    db = f(loc)
    assert b.identity(loc) == {
        "host": "dolt.test",
        "port": 3307,
        "database": loc["url"].rsplit("/", 1)[1],
    }

    clean = b.fingerprint(loc, None)
    assert set(clean) == {"commit"}
    db.write("main")
    dirty = b.fingerprint(loc, None)
    assert dirty == {"commit": clean["commit"], "dirty": True}
    with pytest.raises(BackendError):
        b.pin(loc, dirty, compute_pin_id("dolt", b.identity(loc), dirty))

    db.commit("main", "add rows")
    state = b.fingerprint(loc, None)
    pid = compute_pin_id("dolt", b.identity(loc), state)
    pin = b.pin(loc, state, pid)
    assert pin.ref == ref_for_pin(pid)
    assert b.pin(loc, state, pid) == pin  # idempotent
    with pytest.raises(BackendError):  # ref taken by a different commit
        b.pin(loc, clean, pid)

    # Handles are revision-database URLs without credentials.
    ro = b.open(loc, pin, read_only=True)
    assert isinstance(ro, DoltHandle) and ro.read_only
    assert ro.url == f"mysql://alice@dolt.test:3307/{ro.database}/{pin.ref}"
    assert (
        ro.commit == state["commit"] and ro.database_ref == f"{ro.database}/{pin.ref}"
    )
    at = b.open(loc, state, read_only=True)
    assert isinstance(at, DoltHandle) and at.ref == state["commit"]
    wref = b.fork(loc, pin, "tether.ws.abcd1234.db")
    rw = b.open(loc, wref, read_only=False)
    assert isinstance(rw, DoltHandle) and not rw.read_only and rw.ref == wref

    # Re-forking an existing branch that moved resets it to the pin; an
    # untouched one is left alone.
    assert b.fork(loc, pin, wref) == wref
    db.write(wref)
    db.commit(wref, "diverge")
    assert b.fingerprint(loc, wref) != state
    assert b.fork(loc, pin, wref) == wref
    assert b.fingerprint(loc, wref) == state

    assert b.verify(loc, state, None, deep=False).status is VerifyStatus.UNKNOWN
    assert b.verify(loc, state, None, deep=True).ok
    assert (
        b.verify(loc, {"commit": "f" * 32}, None, deep=True).status
        is VerifyStatus.MISSING
    )
    with pytest.raises(BackendError):
        b.fingerprint(loc, "no-such-branch")
    with pytest.raises(BackendError):
        b.identity({"host": "h"})

    # Content diff: per-table summary + row stats between two commits.
    db.commit("main", "reshape", {"t": 7, "u": 3})
    later = b.fingerprint(loc, None)

    # History and detached bases (`at` = commit hash or tag).
    entries = b.history(loc, None, 10)
    assert [e.id for e in entries][:2] == [later["commit"], state["commit"]]
    assert entries[0].refs == ["main"] and entries[0].message == "reshape"
    assert pin.ref in entries[1].refs
    assert b.fingerprint(dict(loc, at=state["commit"]), None) == state
    assert b.fingerprint(dict(loc, at=pin.ref), None) == state
    with pytest.raises(BackendError):
        b.fingerprint(dict(loc, at="nope"), None)
    at_handle = b.open(dict(loc, at=pin.ref), None, read_only=True)
    assert isinstance(at_handle, DoltHandle) and at_handle.commit == state["commit"]
    d = b.diff(loc, state, later)
    assert d.unit == "tables" and (d.added, d.removed, d.modified) == (1, 0, 1)
    by_path = {e.path: e for e in d.entries}
    assert by_path["u"].change == "added" and by_path["u"].detail == "+3 rows"
    assert by_path["t"].change == "modified" and by_path["t"].detail == "+6 rows"
    assert b.diff(loc, later, later).is_empty
    dirty_note = b.diff(loc, later, dict(later, dirty=True))
    assert dirty_note.is_empty and "dirty" in dirty_note.note


# --------------------------------------------------------------------------- #
# SqlDoltClient issues the expected statements
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self, log: list, rows: list[tuple], columns: list[str]) -> None:
        self._log = log
        self._rows = rows
        self._columns = columns
        self.description: list[tuple] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((sql, tuple(params)))
        self.description = [(c,) for c in self._columns] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Connection:
    def __init__(
        self,
        log: list,
        rows: list[tuple],
        kwargs: dict,
        columns: list[str] | None = None,
    ) -> None:
        self._log = log
        self._rows = rows
        self._columns = columns or ["col"]
        log.append(("CONNECT", tuple(sorted(kwargs.items()))))

    def cursor(self) -> _Cursor:
        return _Cursor(self._log, self._rows, self._columns)

    def close(self) -> None:
        self._log.append(("CLOSE", ()))


def test_sql_client_statements() -> None:
    log: list = []
    rows: list[tuple] = [("abc123", 1)]
    client = SqlDoltClient(
        lambda **kw: _Connection(log, rows, kw), host="h", user="u", database="d"
    )
    assert client.branch_head("main") == ("abc123", True)
    assert log[0] == ("CONNECT", (("database", "d"), ("host", "h"), ("user", "u")))
    assert log[1] == (
        "SELECT hash, dirty FROM dolt_branches WHERE name = %s",
        ("main",),
    )
    assert log[2] == ("CLOSE", ())
    log.clear()

    rows[:] = [("abc123",)]
    assert client.tag_hash("tether.x") == "abc123"
    assert log[1] == (
        "SELECT tag_hash FROM dolt_tags WHERE tag_name = %s",
        ("tether.x",),
    )
    assert client.commit_exists("abc123") is True
    assert log[4] == ("SELECT 1 FROM dolt_commits WHERE commit_hash = %s", ("abc123",))
    log.clear()

    rows[:] = []
    client.create_tag("tether.x", "abc123")
    client.delete_tag("tether.x")
    client.create_branch("b", "tether.x")
    client.delete_branch("b")
    assert client.tag_hash("nope") is None and client.branch_head("nope") is None
    assert client.diff_summary("a", "b") == [] and client.diff_stat("a", "b") == []
    assert client.resolve("main") is None and client.log("main", 5) == []
    statements = [entry for entry in log if entry[0] not in ("CONNECT", "CLOSE")]
    assert statements == [
        ("CALL DOLT_TAG(%s, %s)", ("tether.x", "abc123")),
        ("CALL DOLT_TAG('-d', %s)", ("tether.x",)),
        ("CALL DOLT_BRANCH(%s, %s)", ("b", "tether.x")),
        ("CALL DOLT_BRANCH('-D', %s)", ("b",)),
        ("SELECT tag_hash FROM dolt_tags WHERE tag_name = %s", ("nope",)),
        ("SELECT hash, dirty FROM dolt_branches WHERE name = %s", ("nope",)),
        ("SELECT * FROM dolt_diff_summary(%s, %s)", ("a", "b")),
        ("SELECT * FROM dolt_diff_stat(%s, %s)", ("a", "b")),
        ("SELECT HASHOF(%s)", ("main",)),
        (
            "SELECT commit_hash, committer, date, message FROM DOLT_LOG(%s) LIMIT %s",
            ("main", 5),
        ),
    ]
    # Dict rows come back keyed by cursor description.
    columns = ["table_name", "diff_type", "data_change", "schema_change"]
    client2 = SqlDoltClient(
        lambda **kw: _Connection(log, [("t", "modified", 1, 0)], kw, columns)
    )
    assert client2.diff_summary("a", "b") == [
        {
            "table_name": "t",
            "diff_type": "modified",
            "data_change": 1,
            "schema_change": 0,
        }
    ]


def test_backend_builds_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymysql")
    monkeypatch.setenv("DOLT_PASSWORD", "s3cret")
    monkeypatch.setenv("DOLT_USER", "svc")
    b = DoltBackend()
    client = b._client({"host": "dolt.test", "database": "d"})
    assert isinstance(client, SqlDoltClient)
    assert client._kwargs == {
        "host": "dolt.test",
        "port": 3306,
        "user": "svc",
        "password": "s3cret",
        "database": "d",
        "autocommit": True,
    }
    # The URL handle never carries the password.
    handle = b.open({"host": "dolt.test", "database": "d"}, None, read_only=True)
    assert isinstance(handle, DoltHandle)
    assert handle.url == "mysql://svc@dolt.test:3306/d/main"
