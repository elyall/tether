"""`promote` / `merge` through the lakeFS and Dolt fakes (experimental backends)."""

from __future__ import annotations

import pytest

from tether.errors import BackendError, MergeConflict


def test_lakefs_promote_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from lakefs.exceptions import ConflictException  # noqa: F401
    from test_lakefs_backend import FakeLakeFS

    from tether.experimental.backends.lakefs import LakeFSBackend

    b = LakeFSBackend()
    f = FakeLakeFS()
    monkeypatch.setattr(b, "_repo", f)
    loc = {"repository": f.create_repo(), "branch": "main"}
    base = b.fingerprint(loc, None)
    b.fork(loc, base, "tether.ws.x.lake")
    repo = f(loc)
    repo.branch("tether.ws.x.lake").stage("data/1", b"x")
    repo.branch("tether.ws.x.lake").commit("fork")
    assert b.ancestor_of(loc, base, "tether.ws.x.lake") is True
    new = b.promote(loc, "tether.ws.x.lake")
    assert new == b.fingerprint(loc, None) and new != base
    assert f.repos[loc["repository"]].commits[new["commit_id"]]["data/1"] == b"x"

    repo.branch("tether.ws.x.lake").stage("data/2", b"y")
    repo.branch("tether.ws.x.lake").commit("fork again")
    repo.branch("main").stage("data/3", b"z")
    repo.branch("main").commit("main moved")
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.lake")
    reviewed = b.fingerprint(loc, "tether.ws.x.lake")
    repo.branch("tether.ws.x.lake").stage("data/late", b"late")
    repo.branch("tether.ws.x.lake").commit("after review")
    merged = b.merge(loc, reviewed, "merge")  # the reviewed commit, not the head
    tree = f.repos[loc["repository"]].commits[merged["commit_id"]]
    assert {k for k in tree if k.startswith("data/")} == {"data/1", "data/2", "data/3"}

    repo.branch("tether.ws.x.lake").stage("data/k", b"fork")
    repo.branch("tether.ws.x.lake").commit("k")
    repo.branch("main").stage("data/k", b"main")
    repo.branch("main").commit("k")
    with pytest.raises(MergeConflict):
        b.merge(loc, "tether.ws.x.lake", "boom")


def test_dolt_promote_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_dolt_backend import FakeDolt

    from tether.experimental.backends.dolt import DoltBackend

    b = DoltBackend()
    f = FakeDolt()
    monkeypatch.setattr(b, "_client", f)
    loc = {"database": f.create_db(), "branch": "main", "host": "h"}
    db = f(loc)
    base = b.fingerprint(loc, None)
    b.fork(loc, base, "tether.ws.x.ledger")
    db.commit("tether.ws.x.ledger", "fork", {"t": 5})
    assert b.ancestor_of(loc, base, "tether.ws.x.ledger") is True
    new = b.promote(loc, "tether.ws.x.ledger")
    assert db.branches["main"] == db.branches["tether.ws.x.ledger"]
    assert new["commit"] == db.branches["main"]

    db.commit("tether.ws.x.ledger", "more", {"u": 1})
    db.commit("main", "main moved", {"v": 2})
    with pytest.raises(BackendError, match="not an ancestor"):
        b.promote(loc, "tether.ws.x.ledger")
    reviewed = b.fingerprint(loc, "tether.ws.x.ledger")
    db.commit("tether.ws.x.ledger", "after review", {"late": 1})
    merged = b.merge(loc, reviewed, "merge")  # the reviewed commit, not the head
    assert db.commits[merged["commit"]] == {"t": 5, "u": 1, "v": 2}

    db.commit("tether.ws.x.ledger", "k", {"k": 1})
    db.commit("main", "k", {"k": 2})
    with pytest.raises(MergeConflict) as exc:
        b.merge(loc, "tether.ws.x.ledger", "boom")
    assert exc.value.conflicts == ["k"]
