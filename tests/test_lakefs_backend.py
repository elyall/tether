"""lakeFS backend tests against an in-memory fake of the high-level SDK objects.

The backend talks to ``lakefs.Repository`` / ``Branch`` / ``Tag`` objects only
through the small surface modelled here, so the full Forkable conformance suite
runs without a server.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

import pytest

from tether.backends.base import Capability, ObjectBackend, VerifyStatus
from tether.errors import BackendError
from tether.handles import LakeFSHandle
from tether.manifest import Locator, compute_pin_id, ref_for_pin
from tether.testing import run_conformance

lakefs = pytest.importorskip("lakefs")
from lakefs.exceptions import ConflictException, NotFoundException  # noqa: E402

from tether.backends.lakefs import LakeFSBackend  # noqa: E402


@dataclass
class _Commit:
    id: str


@dataclass
class FakeRepoState:
    commits: dict[str, dict[str, bytes]] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)  # name -> commit id
    staged: dict[str, dict[str, bytes]] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def new_commit(self, tree: dict[str, bytes]) -> str:
        cid = hashlib.sha1(repr(sorted(tree.items())).encode()).hexdigest()
        self.commits[cid] = dict(tree)
        return cid


class FakeRef:
    def __init__(self, state: FakeRepoState, ref: str) -> None:
        self._s = state
        self.id = ref

    def get_commit(self) -> _Commit:
        s = self._s
        cid = s.branches.get(self.id) or s.tags.get(self.id) or self.id
        if cid not in s.commits:
            raise NotFoundException(status=404, reason=f"ref {self.id} not found")
        return _Commit(cid)


class FakeBranch(FakeRef):
    def create(self, source_reference: str, exist_ok: bool = False) -> FakeBranch:
        if self.id in self._s.branches:
            if not exist_ok:
                raise ConflictException(status=409, reason="branch exists")
            return self
        self._s.branches[self.id] = FakeRef(self._s, source_reference).get_commit().id
        return self

    def delete(self) -> None:
        if self.id not in self._s.branches:
            raise NotFoundException(status=404, reason="no branch")
        del self._s.branches[self.id]
        self._s.staged.pop(self.id, None)

    def uncommitted(self, max_amount: int | None = None):
        yield from self._s.staged.get(self.id, {}).items()

    def stage(self, path: str, data: bytes) -> None:
        self._s.staged.setdefault(self.id, {})[path] = data

    def commit(self, message: str) -> FakeRef:
        base = dict(self._s.commits[self._s.branches[self.id]])
        base.update(self._s.staged.pop(self.id, {}))
        base["__msg__"] = message.encode()
        cid = self._s.new_commit(base)
        self._s.branches[self.id] = cid
        return FakeRef(self._s, cid)


class FakeTag(FakeRef):
    def create(self, source_ref: str, exist_ok: bool = False) -> FakeTag:
        if self.id in self._s.tags:
            if not exist_ok:
                raise ConflictException(status=409, reason="tag exists")
            return self
        self._s.tags[self.id] = FakeRef(self._s, source_ref).get_commit().id
        return self

    def delete(self) -> None:
        if self.id not in self._s.tags:
            raise NotFoundException(status=404, reason="no tag")
        del self._s.tags[self.id]


class FakeRepository:
    def __init__(self, state: FakeRepoState) -> None:
        self._s = state

    def branch(self, name: str) -> FakeBranch:
        return FakeBranch(self._s, name)

    def tag(self, name: str) -> FakeTag:
        return FakeTag(self._s, name)

    def tags(self, prefix: str | None = None, **_: object):
        for name in sorted(self._s.tags):
            if prefix is None or name.startswith(prefix):
                yield FakeTag(self._s, name)

    def commit(self, commit_id: str) -> FakeRef:
        return FakeRef(self._s, commit_id)


class FakeLakeFS:
    def __init__(self) -> None:
        self.repos: dict[str, FakeRepoState] = {}

    def create_repo(self) -> str:
        rid = f"repo-{uuid.uuid4().hex[:8]}"
        state = FakeRepoState()
        state.branches["main"] = state.new_commit({})
        self.repos[rid] = state
        return rid

    def __call__(self, locator: Locator) -> FakeRepository:
        return FakeRepository(self.repos[str(locator["repository"])])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> tuple[LakeFSBackend, FakeLakeFS]:
    b = LakeFSBackend()
    f = FakeLakeFS()
    monkeypatch.setattr(b, "_repo", f)
    return b, f


class LakeFSHarness:
    def __init__(self, backend: LakeFSBackend, fake: FakeLakeFS) -> None:
        self.backend: ObjectBackend = backend
        self.fake = fake
        self._n = 0

    def new_object(self) -> Locator:
        return {"repository": self.fake.create_repo(), "branch": "main"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        repo = self.fake(locator)
        branch = repo.branch(working_ref or str(locator.get("branch", "main")))
        branch.stage(f"data/{self._n}.parquet", b"x" * self._n)
        branch.commit(f"mutation {self._n}")


def test_lakefs_conformance(fake: tuple[LakeFSBackend, FakeLakeFS]) -> None:
    b, f = fake
    assert Capability.FORK in b.capabilities
    run_conformance(LakeFSHarness(b, f))


def test_lakefs_dirty_branch_is_reported_and_refused(
    fake: tuple[LakeFSBackend, FakeLakeFS],
) -> None:
    b, f = fake
    loc = {"repository": f.create_repo(), "branch": "main", "prefix": "raw/"}
    clean = b.fingerprint(loc, None)
    assert set(clean) == {"commit_id"}

    f(loc).branch("main").stage("raw/a.bin", b"a")
    dirty = b.fingerprint(loc, None)
    assert dirty["dirty"] is True and dirty["commit_id"] == clean["commit_id"]
    with pytest.raises(BackendError):
        b.pin(loc, dirty, compute_pin_id("lakefs", b.identity(loc), dirty))

    f(loc).branch("main").commit("add a")
    state = b.fingerprint(loc, None)
    pid = compute_pin_id("lakefs", b.identity(loc), state)
    pin = b.pin(loc, state, pid)
    assert pin.ref == ref_for_pin(pid)
    with pytest.raises(BackendError):  # ref taken by a different commit
        b.pin(loc, clean, pid)

    # Handles are lakefs:// URIs scoped by the prefix.
    ro = b.open(loc, pin, read_only=True)
    assert isinstance(ro, LakeFSHandle) and ro.read_only
    assert ro.uri == f"lakefs://{loc['repository']}/{pin.ref}/raw/"
    assert ro.commit_id == state["commit_id"]
    wref = b.fork(loc, pin, "tether.ws.abcd1234.raw")
    rw = b.open(loc, wref, read_only=False)
    assert isinstance(rw, LakeFSHandle) and not rw.read_only
    assert rw.uri == f"lakefs://{loc['repository']}/{wref}/raw/"

    # Re-forking an existing branch that moved resets it to the pin.
    f(loc).branch(wref).stage("raw/b.bin", b"b")
    f(loc).branch(wref).commit("diverge")
    assert b.fingerprint(loc, wref) != state
    assert b.fork(loc, pin, wref) == wref
    assert b.fingerprint(loc, wref) == state

    # Identity is the repository: prefix does not change the pin id.
    assert b.identity(loc) == {"repository": loc["repository"]}
    assert b.verify(loc, state, None, deep=False).status is VerifyStatus.UNKNOWN
    assert b.verify(loc, state, None, deep=True).ok
    assert (
        b.verify(loc, {"commit_id": "0" * 40}, None, deep=True).status
        is VerifyStatus.MISSING
    )
