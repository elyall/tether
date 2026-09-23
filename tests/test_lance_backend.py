from __future__ import annotations

from pathlib import Path

import pytest

from tether.backends.base import ObjectBackend, VerifyStatus
from tether.errors import BackendError
from tether.handles import LanceHandle
from tether.manifest import Locator, compute_pin_id, ref_for_pin, working_ref_name
from tether.testing import run_conformance

pa = pytest.importorskip("pyarrow")
lance = pytest.importorskip("lance")
from tether.backends.lance import LanceBackend  # noqa: E402


def _append(ds, n: int) -> None:
    lance.write_dataset(pa.table({"a": [n]}), ds, mode="append")


class LanceHarness:
    def __init__(self, tmp: Path) -> None:
        self.backend: ObjectBackend = LanceBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        self._n += 1
        uri = str(self.tmp / f"d{self._n}.lance")
        lance.write_dataset(pa.table({"a": [0]}), uri)
        return {"uri": uri}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        ds = lance.dataset(locator["uri"])
        target = (
            ds
            if working_ref in (None, "main")
            else ds.checkout_version((working_ref, None))
        )
        _append(target, self._n)


def test_lance_conformance(tmp_path: Path) -> None:
    run_conformance(LanceHarness(tmp_path))


def test_lance_local_uri_spellings_share_one_identity(tmp_path: Path) -> None:
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    assert b.identity({"uri": f"file://{uri}"}) == b.identity({"uri": uri})
    assert b.identity({"uri": uri}) == {"uri": uri}
    assert b.identity({"uri": "s3://bucket/d.lance"}) == {"uri": "s3://bucket/d.lance"}


def test_lance_fork_lifecycle(tmp_path: Path) -> None:
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1, 2]}), uri)
    loc = {"uri": uri}

    state = b.fingerprint(loc, None)
    assert state == {"branch": "main", "version": 1}
    pid = compute_pin_id("lance", b.identity(loc), state, "d5d5d5d5")
    pin = b.pin(loc, state, pid)
    assert pin.ref == ref_for_pin(pid)
    assert b.pin(loc, state, pid) == pin  # idempotent
    with pytest.raises(BackendError):  # same ref, different target
        b.pin(loc, {"branch": "main", "version": 0}, pid)

    # A fresh fork reports the parent's address, so a no-op commit is a no-op.
    name = working_ref_name("d5d5d5d5", "feature")
    wref = b.fork(loc, pin, name)
    assert wref == name
    assert b.fingerprint(loc, wref) == state

    # Writes on the fork move only the fork.
    handle = b.open(loc, wref, read_only=False)
    assert isinstance(handle, LanceHandle) and not handle.read_only
    _append(handle.dataset, 3)
    forked = b.fingerprint(loc, wref)
    assert forked == {"branch": wref, "version": 2, "branch_id": forked["branch_id"]}
    assert b.fingerprint(loc, None) == state

    # Pin the fork's state: the tag names (branch, version) on the fork.
    pid2 = compute_pin_id("lance", b.identity(loc), forked, "d5d5d5d5")
    pin2 = b.pin(loc, forked, pid2)
    ro = b.open(loc, pin2, read_only=True)
    assert isinstance(ro, LanceHandle) and ro.read_only and ro.tag == pin2.ref
    assert ro.dataset.to_table().to_pydict() == {"a": [1, 2, 3]}
    assert b.verify(loc, forked, pin2, deep=True).ok
    assert {pid, pid2} <= b.list_pins(loc)

    # Lance keeps a tagged branch alive; delete_working_ref says so rather than
    # claiming a deletion, and re-forking under the same name picks a sibling.
    with pytest.raises(BackendError, match="keeps a branch while a tag"):
        b.delete_working_ref(loc, wref)
    assert wref in lance.dataset(uri).branches.list()
    wref2 = b.fork(loc, pin, name)
    assert wref2 == f"{name}.2"
    assert b.fingerprint(loc, wref2) == state

    # Once the tag is gone the branch can be dropped and the pin is missing.
    b.unpin(loc, pin2)
    assert pid2 not in b.list_pins(loc)
    assert b.verify(loc, forked, pin2, deep=False).status is VerifyStatus.MISSING
    b.delete_working_ref(loc, wref)
    assert wref not in lance.dataset(uri).branches.list()

    # Addressable reads by (branch, version) and drift detection on the tag.
    at = b.open(loc, state, read_only=True)
    assert isinstance(at, LanceHandle) and at.version == 1
    assert b.verify(loc, {"branch": "main", "version": 0}, pin, False).status is (
        VerifyStatus.DRIFTED
    )
    assert b.verify(loc, state, None, True).ok
    assert (
        b.verify(loc, {"branch": "main", "version": 42}, None, True).status
        is VerifyStatus.MISSING
    )
    with pytest.raises(BackendError):
        b.fingerprint({"uri": str(tmp_path / "absent.lance")}, None)


def _rows(uri: str, branch: str) -> list[int]:
    return (
        lance.dataset(uri).checkout_version((branch, None)).to_table()["a"].to_pylist()
    )


def test_lance_racing_absent_forks_create_the_branch_once(tmp_path: Path) -> None:
    """Port of the review's D4: a fork that found the branch absent listed,
    deleted and re-created a branch of that name, so a peer's branch made in
    between was replaced. `create_branch` refuses a name that exists, which
    makes it the whole check: of racing forks one creates the branch, the
    others are refused, and what the winner writes stays."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tether.backends.base import ABSENT
    from tether.errors import RefMovedError

    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1]}), uri)
    loc = {"uri": uri}
    source = LanceBackend().fingerprint(loc, None)
    name = working_ref_name("d5d5d5d5", "shared")
    racers = 8
    barrier = threading.Barrier(racers, timeout=60)

    def fork_once(_: int) -> str:
        b = LanceBackend()
        barrier.wait()
        try:
            ref = b.fork(loc, source, name, expected=ABSENT)
        except RefMovedError:
            return "refused"
        _append(lance.dataset(uri).checkout_version((ref, None)), 2)
        return "won"

    with ThreadPoolExecutor(max_workers=racers) as pool:
        outcomes = list(pool.map(fork_once, range(racers)))
    assert outcomes.count("won") == 1 and outcomes.count("refused") == racers - 1
    assert list(lance.dataset(uri).branches.list()) == [name]
    assert _rows(uri, name) == [1, 2]  # the winner's write survived the losers


@pytest.mark.parametrize("after", range(1, 5))
@pytest.mark.parametrize("read", ["open", "list"])
@pytest.mark.parametrize("peer", ["writes", "recreates", "creates"])
def test_lance_conditional_fork_never_replaces_a_branch_it_did_not_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: str, read: str, after: int
) -> None:
    """A peer acts on the branch at every point of a conditional fork -- right
    after its `after`-th dataset open or branch listing, or once it returned
    -- and its write is never lost: the fork either refuses (`RefMovedError`)
    or ran before the peer. The old fork checked the head first and then
    opened, listed and deleted, so a peer landing after the check was
    replaced. Lance has no conditional delete, so the head check itself is
    the one place left: it is the last read before the delete (a peer inside
    it is not injected here). `creates`: the branch was absent when reviewed
    (`ABSENT`), and creating is the check; `recreates`: deleted and made
    anew, so its version number matches and only the branch id differs."""
    import contextlib

    from tether.backends import lance as lance_module
    from tether.backends.base import ABSENT
    from tether.errors import RefMovedError

    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1]}), uri)
    loc = {"uri": uri}
    b = LanceBackend()
    source = b.fingerprint(loc, None)
    name = working_ref_name("d5d5d5d5", "feat")
    if peer == "creates":
        expected = ABSENT
    else:
        b.fork(loc, source, name)
        _append(lance.dataset(uri).checkout_version((name, None)), 2)
        expected = b.fingerprint(loc, name)

    def act() -> None:
        ds = lance.dataset(uri)
        if peer == "recreates":
            ds.branches.delete(name)
        with contextlib.suppress(OSError):  # a branch the fork made first
            if peer in ("creates", "recreates"):
                ds.create_branch(name, (None, None))
        _append(ds.checkout_version((name, None)), 99)

    calls: list[None] = []
    checking: list[bool] = []

    def maybe_act() -> None:
        if checking:
            return
        calls.append(None)
        if len(calls) == after:
            act()

    real_check = lance_module.check_expected

    def head_check(*args, **kwargs):
        checking.append(True)
        try:
            return real_check(*args, **kwargs)
        finally:
            checking.clear()

    monkeypatch.setattr(lance_module, "check_expected", head_check)
    if read == "open":
        real_dataset = LanceBackend._dataset

        def dataset_then_peer(self, locator):
            ds = real_dataset(self, locator)
            maybe_act()
            return ds

        monkeypatch.setattr(LanceBackend, "_dataset", dataset_then_peer)
    else:
        branches_type = type(lance.dataset(uri).branches)
        real_list = branches_type.list

        def list_then_peer(self):
            listed = real_list(self)
            maybe_act()
            return listed

        monkeypatch.setattr(branches_type, "list", list_then_peer)
    with contextlib.suppress(RefMovedError):
        b.fork(loc, source, name, expected=expected)
    fired = len(calls) >= after
    monkeypatch.undo()
    if not fired:
        act()  # the peer comes after the fork
    assert _rows(uri, name)[-1] == 99


def test_lance_recreated_branch_is_a_new_state(tmp_path: Path) -> None:
    # A branch deleted and forked again under its name restarts its version
    # numbers, so (branch, version) alone names two contents.
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1]}), uri)
    loc = {"uri": uri}
    base = b.fingerprint(loc, None)
    name = working_ref_name("d5d5d5d5", "feat")

    def rows(state: dict) -> list[int]:
        handle = b.open(loc, state, read_only=True)
        assert isinstance(handle, LanceHandle)
        return handle.dataset.to_table()["a"].to_pylist()

    w1 = b.fork(loc, base, name)
    _append(lance.dataset(uri).checkout_version((w1, None)), 2)
    s1 = b.fingerprint(loc, w1)
    assert rows(s1) == [1, 2]
    b.delete_working_ref(loc, w1)

    w2 = b.fork(loc, base, name)
    assert w2 == w1
    lance.write_dataset(
        pa.table({"a": [777, 888]}),
        lance.dataset(uri).checkout_version((w2, None)),
        mode="append",
    )
    s2 = b.fingerprint(loc, w2)
    assert (s2["branch"], s2["version"]) == (s1["branch"], s1["version"])
    assert s2 != s1 and rows(s2) == [1, 777, 888]

    for deep in (False, True):
        report = b.verify(loc, s1, None, deep=deep)
        assert report.status is VerifyStatus.MISSING and "re-created" in report.message
    assert b.verify(loc, s2, None, deep=True).ok
    for use in (
        lambda: b.open(loc, s1, read_only=True),
        lambda: b.fork(loc, s1, working_ref_name("d5d5d5d5", "other")),
        lambda: b.pin(loc, s1, "aaaa1111bbbb"),
        lambda: b.diff(loc, base, s1),
    ):
        with pytest.raises(BackendError, match="re-created"):
            use()


def test_lance_diff_reports_fragments_and_columns(tmp_path: Path) -> None:
    b = LanceBackend()
    uri = str(tmp_path / "d.lance")
    lance.write_dataset(pa.table({"a": [1, 2, 3]}), uri)
    loc = {"uri": uri}
    s1 = b.fingerprint(loc, None)
    _append(lance.dataset(uri), 4)
    lance.dataset(uri).delete("a = 2")
    s3 = b.fingerprint(loc, None)
    d = b.diff(loc, s1, s3)
    assert d.unit == "fragments" and (d.added, d.removed, d.modified) == (1, 0, 1)
    assert {(e.path, e.change, e.detail) for e in d.entries} == {
        ("fragment 1", "added", "+1 rows"),
        ("fragment 0", "modified", "deletions changed"),
    }
    lance.dataset(uri).add_columns({"b": "a * 2"})
    s4 = b.fingerprint(loc, None)
    d2 = b.diff(loc, s3, s4)
    assert [(e.path, e.change) for e in d2.entries] == [("column b", "added")]
    assert b.diff(loc, s4, s4).is_empty

    # History lists versions newest first with the branch head marked; `at`
    # takes a version number or a tag on the base branch.
    b.pin(loc, s1, "aaaa1111bbbb")
    log = b.history(loc, None, 10)
    assert [e.id for e in log] == ["4", "3", "2", "1"]
    assert log[0].refs == ["main"] and log[-1].refs == ["tether.aaaa1111bbbb"]
    assert all(e.when for e in log)
    assert b.fingerprint(dict(loc, at="1"), None) == s1
    assert b.fingerprint(dict(loc, at="tether.aaaa1111bbbb"), None) == s1
    assert b.history(loc, None, 2) == log[:2]
    with pytest.raises(BackendError):
        b.fingerprint(dict(loc, at="not-a-tag"), None)
