from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from tether.backends.base import (
    ABSENT,
    Capability,
    ObjectBackend,
    check_expected,
    fork_ref,
    merge_ref,
    promote_ref,
)
from tether.backends.memory import MemoryBackend, MemoryStore
from tether.errors import RefMovedError
from tether.manifest import Locator
from tether.testing import run_conformance


class MemoryHarness:
    def __init__(self) -> None:
        self.store = MemoryStore()
        self.backend: ObjectBackend = MemoryBackend(self.store)
        self._n = 0

    def new_object(self) -> Locator:
        name = f"sys-{uuid.uuid4().hex[:8]}"
        self.store.system(name)  # materialize with a main branch
        return {"system": name, "branch": "main"}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        branch = working_ref or locator.get("branch", "main")
        # A new key per write, on top of what the branch holds: two branches
        # written this way merge without conflict, which the MERGE checks need.
        payload = self.store.read(locator["system"], branch)
        payload[f"n{self._n}"] = self._n
        self.store.write(locator["system"], branch, payload)

    def fresh_locator(self) -> Locator:
        return {"system": f"sys-{uuid.uuid4().hex[:8]}", "branch": "main"}


class LocalFileHarness:
    capabilities = Capability.FINGERPRINT | Capability.CHEAP_FINGERPRINT

    def __init__(self, tmp: Path) -> None:
        from tether.backends.file import FileBackend

        self.backend: ObjectBackend = FileBackend()
        self.tmp = tmp
        self._n = 0

    def new_object(self) -> Locator:
        p = self.tmp / f"f-{uuid.uuid4().hex[:8]}.txt"
        p.write_text("v0", encoding="utf-8")
        return {"uri": str(p)}

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        self._n += 1
        target = Path(locator["uri"])
        if target.is_dir():  # a created directory: add a file to it
            (target / f"part-{self._n}.txt").write_text("x" * self._n, encoding="utf-8")
            return
        target.write_text(f"v{self._n}" * (self._n + 1), encoding="utf-8")

    def fresh_locator(self) -> Locator:
        return {"uri": str(self.tmp / f"dir-{uuid.uuid4().hex[:8]}")}


def test_memory_backend_conformance() -> None:
    run_conformance(MemoryHarness())


def test_file_backend_conformance(tmp_path: Path) -> None:
    run_conformance(LocalFileHarness(tmp_path))


# --------------------------------------------------------------------------- #
# CONDITIONAL_REF: what a backend claims about its ref moves
# --------------------------------------------------------------------------- #
class _IgnoresExpected(MemoryBackend):
    """Takes `expected` on the named methods and drops it."""

    ignored: frozenset[str] = frozenset()

    def fork(self, locator, source, name, *, expected=None):
        if "fork" in self.ignored:
            expected = None
        return super().fork(locator, source, name, expected=expected)

    def promote(self, locator, source, *, expected=None):
        if "promote" in self.ignored:
            expected = None
        return super().promote(locator, source, expected=expected)

    def merge(self, locator, source, message, *, expected=None):
        if "merge" in self.ignored:
            expected = None
        return super().merge(locator, source, message, expected=expected)


class _NoKeyword(MemoryBackend):
    """A backend written before `expected`: none of its moves take it (the
    narrower signatures are the point, hence the ignores)."""

    def fork(self, locator, source, name):  # ty: ignore[invalid-method-override]
        return super().fork(locator, source, name)

    def promote(self, locator, source):  # ty: ignore[invalid-method-override]
        return super().promote(locator, source)

    def merge(self, locator, source, message):  # ty: ignore[invalid-method-override]
        return super().merge(locator, source, message)


class _CheckThenAct(MemoryBackend):
    """Compares the head, then moves it a moment later: sequentially right,
    but two racing writers both pass the check."""

    def fork(self, locator, source, name, *, expected=None):
        check_expected(self, locator, expected, ref=name)
        time.sleep(0.02)
        return super().fork(locator, source, name)


def _harness(backend: MemoryBackend) -> MemoryHarness:
    h = MemoryHarness()
    backend.store = h.store
    h.backend = backend
    return h


@pytest.mark.parametrize("method", ["fork", "promote", "merge"])
def test_conformance_fails_a_backend_that_declares_conditional_ref_and_ignores_expected(
    method: str,
) -> None:
    backend = _IgnoresExpected()
    backend.ignored = frozenset({method})
    assert Capability.CONDITIONAL_REF in backend.capabilities
    with pytest.raises(AssertionError, match="stale expected"):
        run_conformance(_harness(backend))


def test_conformance_fails_a_conditional_ref_backend_without_the_keyword() -> None:
    with pytest.raises(AssertionError, match=r"declares CONDITIONAL_REF.*fork\(\)"):
        run_conformance(_harness(_NoKeyword()))


def test_conformance_fails_a_conditional_ref_that_is_check_then_act() -> None:
    with pytest.raises(AssertionError, match=r"racing forks that create"):
        run_conformance(_harness(_CheckThenAct()))


def test_conformance_warns_for_unconditional_moves_without_the_capability() -> None:
    class Legacy(_NoKeyword):
        capabilities = MemoryBackend.capabilities & ~Capability.CONDITIONAL_REF

    with pytest.warns(RuntimeWarning, match="unconditional") as seen:
        run_conformance(_harness(Legacy()))
    assert {str(w.message).split("(")[0] for w in seen} >= {
        "memory.fork",
        "memory.promote",
        "memory.merge",
    }

    # Check-then-act without the claim passes: the sequential checks hold.
    class Honest(_CheckThenAct):
        capabilities = MemoryBackend.capabilities & ~Capability.CONDITIONAL_REF

    run_conformance(_harness(Honest()))


@pytest.mark.parametrize("move", ["fork", "promote", "merge"])
def test_the_engine_checks_and_warns_for_a_backend_without_expected(move: str) -> None:
    """A backend predating `expected` is not trusted with a conditional move
    it cannot make: the engine compares the head itself (refusing a stale
    one, as the backend would have) and warns that the move is not
    conditional. It used to call the backend unconditionally, in silence."""
    h = _harness(_NoKeyword())
    b, loc = h.backend, h.new_object()
    h.mutate(loc, None)
    base = b.fingerprint(loc, None)
    wref = b.fork(loc, base, "tether.ws.c0fe5a1e.legacy")
    h.mutate(loc, wref)
    head = b.fingerprint(loc, wref)
    if move == "fork":
        stale, fresh = ABSENT, head

        def act(expected):
            return fork_ref(b, loc, base, wref, expected)

        where = wref
    else:
        stale, fresh = head, base

        def act(expected):
            if move == "promote":
                return promote_ref(b, loc, wref, expected)
            return merge_ref(b, loc, wref, "legacy merge", expected)

        where = None
    before = b.fingerprint(loc, where)
    with (
        pytest.warns(RuntimeWarning, match=f"{move}\\(\\) takes no `expected`"),
        pytest.raises(RefMovedError),
    ):
        act(stale)
    assert b.fingerprint(loc, where) == before  # nothing moved
    with pytest.warns(RuntimeWarning, match="not conditional"):
        act(fresh)
    assert b.fingerprint(loc, where) != before
