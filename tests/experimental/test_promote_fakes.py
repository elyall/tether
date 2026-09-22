"""`promote` / `merge` through the Dolt fake (experimental backend)."""

from __future__ import annotations

import pytest

from tether.errors import BackendError, MergeConflict


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
