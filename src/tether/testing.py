"""Importable, capability-parametrized conformance suite for backends.

A backend author implements a small :class:`BackendHarness` (create a fresh
object; mutate its current state) and calls :func:`run_conformance`. The suite
runs only the checks appropriate to the backend's declared capabilities, so the
same spec validates an Observed file backend and a Forkable Icechunk backend.
"""

from __future__ import annotations

import threading
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, runtime_checkable

from tether.backends.base import (
    ABSENT,
    Capability,
    ObjectBackend,
    ObjectDiff,
    VerifyStatus,
    accepts_expected,
    content_state,
    effective_capabilities,
)
from tether.errors import BackendError, RefMovedError
from tether.handles import Handle
from tether.manifest import (
    Locator,
    Pin,
    Policy,
    compute_pin_id,
    ref_for_pin,
    working_ref_name,
)

__all__ = ["BackendHarness", "run_conformance"]

CONFORMANCE_DATASET = "c0fe5a1e"
"""Dataset id the conformance suite pins under (any 8 hex chars would do)."""


@runtime_checkable
class BackendHarness(Protocol):
    """Test fixture a backend author provides to drive the suite."""

    backend: ObjectBackend

    def new_object(self) -> Locator:
        """Create a fresh, empty system and return a locator for it."""

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        """Cause the state at ``working_ref`` (or the base) to change.

        Every call writes something new, and for a ``MERGE`` backend two calls
        on different branches write *different* things (a new file, table, or
        key per call): the suite merges a fork into a base both were written
        to, and expects no conflict.
        """

    # A ``CREATE`` backend's harness also provides
    # ``fresh_locator(self) -> Locator``: a locator where no store exists yet.


def _same(b: ObjectBackend, x: dict, y: dict) -> bool:
    """Equality up to `VOLATILE_KEYS`."""
    return content_state(b, x) == content_state(b, y)


def _refuses(
    action: Callable[[], object], error: type[BaseException], message: str
) -> None:
    """`action()` must raise `error` (a subclass counts); `message` names the
    contract it would have broken."""
    try:
        action()
    except error:
        return
    except Exception as exc:  # pragma: no cover - reported to the author
        raise AssertionError(f"{message}, got {type(exc).__name__}: {exc}") from exc
    raise AssertionError(message)


def _content_checks(h: BackendHarness, loc: Locator, s1: dict, s2: dict) -> None:
    """Volatile keys must never be the only thing that changed."""
    b = h.backend
    c1, c2 = content_state(b, s1), content_state(b, s2)
    assert c1 != c2, "a real change must survive content_state"
    for key in b.VOLATILE_KEYS:
        assert key not in (c1 or {}), f"volatile key {key!r} leaked into content"


def _fingerprint_checks(h: BackendHarness, loc: Locator) -> tuple[dict, dict]:
    b = h.backend
    s1 = b.fingerprint(loc, None)
    assert isinstance(s1, dict), "fingerprint must return a dict"
    # Stability is a *content* property: address keys the backend declares
    # volatile (a Neon LSN that moves on checkpoints) may differ between reads.
    assert content_state(b, b.fingerprint(loc, None)) == content_state(b, s1), (
        "fingerprint must be stable (up to VOLATILE_KEYS)"
    )
    h.mutate(loc, None)
    s2 = b.fingerprint(loc, None)
    assert content_state(b, s2) != content_state(b, s1), (
        "fingerprint must change after a mutation"
    )
    return s1, s2


def _history_checks(h: BackendHarness, loc: Locator, s1: dict, s2: dict) -> None:
    b = h.backend
    entries = b.history(loc, None, 10)
    assert entries, "history must list at least the current state"
    assert all(isinstance(e.id, str) and e.id for e in entries), "ids must be strings"
    # The newest entry is the current state, addressable through `at`.
    detached = dict(loc, at=entries[0].id)
    assert b.fingerprint(detached, None) == s2, "history[0] must be the current state"
    # An older entry (when there is one) resolves to a different state.
    if len(entries) > 1:
        older = b.fingerprint(dict(loc, at=entries[1].id), None)
        assert older != s2, "older history entries must be distinct states"
    assert b.history(loc, None, 1) == entries[:1], "limit must bound the result"


def _diff_checks(h: BackendHarness, loc: Locator, s1: dict, s2: dict) -> None:
    b = h.backend
    listings = (b.listing(loc, s1), b.listing(loc, s2))
    same = b.diff(loc, s2, s2, listings=(listings[1], listings[1]))
    assert isinstance(same, ObjectDiff), "diff must return an ObjectDiff"
    assert same.is_empty, "diffing a state against itself must be empty"
    changed = b.diff(loc, s1, s2, listings=listings)
    assert not changed.is_empty, "diff between distinct states must report change"
    assert changed.summary, "diff must have a summary"
    for entry in changed.entries:
        assert entry.change in ("added", "removed", "modified", "renamed")


def _identity_checks(h: BackendHarness, loc: Locator, state: dict) -> None:
    b = h.backend
    ident = b.identity(loc)
    assert isinstance(ident, dict) and ident, "identity must be a non-empty dict"
    content = content_state(b, state)
    assert content is not None
    pid1 = compute_pin_id(b.kind, ident, content, CONFORMANCE_DATASET)
    pid2 = compute_pin_id(b.kind, b.identity(loc), content, CONFORMANCE_DATASET)
    assert pid1 == pid2, "pin id must be deterministic"


def _pin_checks(h: BackendHarness, loc: Locator, state: dict) -> str:
    b = h.backend
    # Pin ids hash the content state (what the engine does); pin() gets the
    # full state, volatile address keys included.
    content = content_state(b, state)
    assert content is not None
    pid = compute_pin_id(b.kind, b.identity(loc), content, CONFORMANCE_DATASET)
    pin = b.pin(loc, state, pid)
    assert pin.ref.startswith(ref_for_pin("")), "pin ref must carry the prefix"
    assert pin.created, "a fresh pin must report created=True"
    assert pid in b.list_pins(loc), "list_pins must include a fresh pin"
    assert b.verify(loc, state, pin, deep=False).ok, "fresh pin must verify"
    assert b.verify(loc, state, pin, deep=True).ok, "deep verify must pass"
    # Idempotent: re-pinning identical state returns the same ref, and says
    # it found the ref rather than made it (the engine must not roll it back).
    again = b.pin(loc, state, pid)
    assert again.ref == pin.ref, "pin must be idempotent"
    assert not again.created, "a re-pin must report created=False"
    # The pin protects state against later drift of the working ref.
    h.mutate(loc, None)
    assert b.verify(loc, state, pin, deep=False).ok, "pin must protect state"
    # The same id for a *different* state is a contradiction the backend must
    # not paper over by moving or reusing the ref.
    other = b.fingerprint(loc, None)
    assert content_state(b, other) != content, "mutate must change the state"
    try:
        b.pin(loc, other, pid)
    except BackendError:
        pass
    else:
        raise AssertionError("pin(same id, other state) must raise BackendError")
    assert b.verify(loc, state, pin, deep=False).ok, "the original pin must survive"
    # Operations on a ref that does not exist raise BackendError, not a
    # library exception and not a silent success.
    ghost = Pin(id="0" * 16, ref=ref_for_pin("0" * 16 + ".ghost"))
    try:
        b.open(loc, ghost, read_only=True)
    except BackendError:
        pass
    except Exception as exc:  # pragma: no cover - reported to the author
        raise AssertionError(
            "open(missing pin) must raise BackendError, got "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    else:
        raise AssertionError("open(missing pin) must raise BackendError")
    try:
        b.unpin(loc, ghost)  # already gone: a no-op or a BackendError
    except BackendError:
        pass
    except Exception as exc:  # pragma: no cover - reported to the author
        raise AssertionError(
            "unpin(missing pin) must be a no-op or raise BackendError, got "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return pid


def _fork_checks(h: BackendHarness, loc: Locator, state: dict, pid: str) -> None:
    b = h.backend

    def same(x: dict, y: dict) -> bool:  # equality up to VOLATILE_KEYS
        return content_state(b, x) == content_state(b, y)

    pin = b.pin(loc, state, pid)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance")
    wref = b.fork(loc, pin, name)
    assert isinstance(wref, str) and wref, "fork must return a working ref"
    forked = b.fingerprint(loc, wref)
    assert same(forked, state), "a fresh fork must start at the pinned state"
    handle = b.open(loc, wref, read_only=False)
    assert isinstance(handle, Handle) and not handle.read_only
    # Writing to the fork changes only the fork.
    base_before = b.fingerprint(loc, None)
    h.mutate(loc, wref)
    assert not same(b.fingerprint(loc, wref), forked), "writes to a fork must register"
    assert same(b.fingerprint(loc, None), base_before), "fork must isolate the base"
    # Reset contract: forking onto an existing name moves it back to the source.
    again = b.fork(loc, pin, name)
    assert same(b.fingerprint(loc, again), state), (
        "fork onto an existing name must reset the branch to the source"
    )
    wref = again
    # ...and the other half: a branch already at the source is left alone --
    # the name comes back unchanged and nothing new appears in the listing.
    listed_before = set(b.list_working_refs(loc))
    assert b.fork(loc, pin, wref) == wref, "fork onto an at-source branch keeps it"
    assert set(b.list_working_refs(loc)) == listed_before, (
        "fork onto an at-source branch must not create a sibling"
    )
    assert same(b.fingerprint(loc, wref), state)
    listed = b.list_working_refs(loc)
    assert wref in listed, f"list_working_refs must include {wref!r} (got {listed})"
    b.delete_working_ref(loc, wref)
    assert wref not in b.list_working_refs(loc), "deleted working ref still listed"
    # Pin-less fork: straight from the recorded state (policy.pin = "record").
    name2 = working_ref_name(CONFORMANCE_DATASET, "conformance-pinless")
    wref2 = b.fork(loc, state, name2)
    assert same(b.fingerprint(loc, wref2), state), "fork from state must start there"
    b.delete_working_ref(loc, wref2)


def _create_checks(h: BackendHarness) -> None:
    """The store lifecycle: create -> own -> empty -> not empty -> empty again
    -> delete -> gone -> create again."""
    b = h.backend
    fresh = getattr(h, "fresh_locator", None)
    assert callable(fresh), (
        f"{b.kind} declares CREATE: its harness must provide fresh_locator()"
    )
    loc = fresh()
    if Capability.CREATE not in effective_capabilities(b, loc, Policy()):
        # Declared, but not for this locator or this library version (an
        # icechunk without repository metadata): the engine would refuse
        # `add --create` the same way, so there is nothing to walk.
        return
    owner = "0a1b2c3d"
    state = b.create(loc, owner=owner)
    assert isinstance(state, dict), "create must return the initial state"
    assert b.owner(loc) == owner, "owner marker must name the creator"
    assert b.is_ref_empty(loc) is True, "a fresh store is ref-empty"
    assert content_state(b, b.fingerprint(loc, None)) == content_state(b, state)
    try:
        b.create(loc, owner="ffffffff")
    except BackendError:
        pass
    else:
        raise AssertionError("create must refuse a store that already exists")
    assert b.owner(loc) == owner, "a refused create must not re-own the store"
    if Capability.FORK in b.capabilities:
        # Writes on a fork make it not empty; deleting the fork makes it empty
        # again -- and the plan can say so ahead of time with `ignoring`.
        name = working_ref_name(CONFORMANCE_DATASET, "create-probe")
        wref = b.fork(loc, state, name)
        h.mutate(loc, wref)
        assert b.is_ref_empty(loc) is False, "a written fork means not empty"
        assert b.is_ref_empty(loc, ignoring={wref}) is True, (
            "ignoring the fork the plan deletes, the store is empty"
        )
        b.delete_working_ref(loc, wref)
        assert b.is_ref_empty(loc) is True
    else:
        h.mutate(loc, None)
        assert b.is_ref_empty(loc) is False, "a written base means not empty"
    if b.is_ref_empty(loc):
        b.delete_store(loc)
        assert b.owner(loc) is None, "a deleted store has no owner"
        try:
            b.fingerprint(loc, None)
        except BackendError:
            pass
        else:
            raise AssertionError("fingerprint of a deleted store must raise")
        again = b.create(loc, owner=owner)  # the name is free again
        assert content_state(b, again) == content_state(b, state)
        b.delete_store(loc)


def _address(handle: Handle) -> dict[str, Any]:
    """The plain-valued fields of a handle: its native address (a sha, a
    snapshot id, a version, a URL), without the live objects (sessions,
    tables, connections) that differ between any two handles anyway."""
    return {
        k: v
        for k, v in vars(handle).items()
        if k not in ("read_only", "key")
        and isinstance(v, str | int | float | bool | type(None))
    }


def _close(handle: Handle) -> None:
    closer = getattr(handle, "close", None)
    if callable(closer):  # a handle that owns a connection (DuckLake)
        closer()


def _addressable_checks(
    h: BackendHarness, loc: Locator, before: dict, state: dict
) -> None:
    b = h.backend
    assert b.verify(loc, state, None, deep=False).status in (
        VerifyStatus.OK,
        VerifyStatus.UNKNOWN,
    ), "addressable state should verify (or be unknown without --deep)"
    # The *older* state is a recorded state too: reading it back is the whole
    # point of the tier, and a handle on it names another address than one on
    # the current state.
    assert b.verify(loc, before, None, deep=True).status in (
        VerifyStatus.OK,
        VerifyStatus.UNKNOWN,
    ), "an older recorded state must still deep-verify"
    handle = b.open(loc, state, read_only=True)
    assert isinstance(handle, Handle) and handle.read_only
    current = _address(handle)
    _close(handle)
    older = b.open(loc, before, read_only=True)
    assert isinstance(older, Handle) and older.read_only
    past = _address(older)
    _close(older)
    assert current != past, (
        f"handles on two states must name two addresses (both {current})"
    )


def _unpin_checks(h: BackendHarness, loc: Locator, state: dict, pid: str) -> None:
    b = h.backend
    pin = b.pin(loc, state, pid)
    ro = b.open(loc, pin, read_only=True)
    assert isinstance(ro, Handle) and ro.read_only
    b.unpin(loc, pin)
    assert pid not in b.list_pins(loc), "unpin must drop the pin id"
    assert b.verify(loc, state, pin, deep=False).status is VerifyStatus.MISSING


def _takes_expected(b: ObjectBackend, method: str, conditional: bool) -> bool:
    """Whether `method` takes `expected`. A backend declaring
    `CONDITIONAL_REF` must; one that does not moves unconditionally, which
    passes with a warning -- the engine warns the same way at every move."""
    if accepts_expected(getattr(b, method)):
        return True
    if conditional:
        raise AssertionError(
            f"{b.kind} declares CONDITIONAL_REF, but its {method}() takes no `expected`"
        )
    warnings.warn(
        f"{b.kind}.{method}() takes no `expected`: its ref moves are "
        "unconditional, and a concurrent write is overwritten",
        RuntimeWarning,
        stacklevel=3,
    )
    return False


def _conditional_fork_checks(
    h: BackendHarness, loc: Locator, *, conditional: bool
) -> None:
    """`fork(..., expected=)`: the head the caller reviewed must still be the
    branch's head (or the branch must not exist, for `ABSENT`) for the move
    to happen; otherwise `RefMovedError`, and the branch stays where it was."""
    b = h.backend
    if not _takes_expected(b, "fork", conditional):
        return
    source = b.fingerprint(loc, None)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance-cas")
    wref = b.fork(loc, source, name)
    h.mutate(loc, wref)
    head = b.fingerprint(loc, wref)
    # Stale: the branch was reviewed at the source and has moved on since.
    _refuses(
        lambda: b.fork(loc, source, wref, expected=source),
        RefMovedError,
        "fork with a stale expected must raise RefMovedError",
    )
    assert _same(b, b.fingerprint(loc, wref), head), (
        "a refused fork must leave the branch where it was"
    )
    _refuses(
        lambda: b.fork(loc, source, wref, expected=ABSENT),
        RefMovedError,
        "fork with expected=ABSENT onto an existing branch must raise RefMovedError",
    )
    assert _same(b, b.fingerprint(loc, wref), head)
    fresh = working_ref_name(CONFORMANCE_DATASET, "conformance-cas-fresh")
    wref2 = b.fork(loc, source, fresh, expected=ABSENT)
    assert _same(b, b.fingerprint(loc, wref2), source), (
        "fork with expected=ABSENT onto a fresh name must create it at the source"
    )
    b.delete_working_ref(loc, wref2)
    again = b.fork(loc, source, wref, expected=head)
    assert _same(b, b.fingerprint(loc, again), source), (
        "fork with the right expected must move the branch back to the source"
    )
    b.delete_working_ref(loc, again)


RACERS = 8
"""Threads the `CONDITIONAL_REF` race checks start at once."""


def _race(attempt: Callable[[], object]) -> list[BaseException | None]:
    """Run `attempt` in `RACERS` threads released together; per thread the
    exception it raised, or `None` for a move that went through."""
    barrier = threading.Barrier(RACERS, timeout=60)

    def one() -> BaseException | None:
        barrier.wait()
        try:
            attempt()
        except BackendError as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        return list(pool.map(lambda _: one(), range(RACERS)))


def _conditional_race_checks(h: BackendHarness, loc: Locator) -> None:
    """`CONDITIONAL_REF`: the head check and the move are one step, so of
    several writers holding the same `expected`, exactly one moves the ref
    and the rest are refused -- creating a branch (`ABSENT`) and resetting
    one from the head they all read."""
    b = h.backend
    source = b.fingerprint(loc, None)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance-race")
    for expected, what in ((ABSENT, "create"), (None, "reset")):
        if expected is None:
            h.mutate(loc, name)
            expected = b.fingerprint(loc, name)
        outcomes = _race(
            lambda expected=expected: b.fork(loc, source, name, expected=expected)
        )
        won = [o for o in outcomes if o is None]
        assert len(won) == 1, (
            f"{len(won)} of {RACERS} racing forks that {what} one branch from the "
            "same `expected` went through; a conditional move lets exactly one"
        )
        assert _same(b, b.fingerprint(loc, name), source)
    b.delete_working_ref(loc, name)


def _ancestry_checks(h: BackendHarness, loc: Locator) -> None:
    """`ancestor_of`: the fork point is in the fork's history, the fork's head
    is not in the fork point's. A backend that leaves the default in place
    answers `None` (unknown) to both."""
    b = h.backend
    source = b.fingerprint(loc, None)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance-ancestry")
    wref = b.fork(loc, source, name)
    h.mutate(loc, wref)
    head = b.fingerprint(loc, wref)
    forward = b.ancestor_of(loc, source, wref)
    backward = b.ancestor_of(loc, head, source)
    if type(b).ancestor_of is ObjectBackend.ancestor_of:
        assert forward is None and backward is None, (
            "the default ancestor_of answers None"
        )
    else:
        assert forward is True, "the fork point must be an ancestor of the fork"
        assert backward is False, "the fork's head is not an ancestor of the fork point"
    b.delete_working_ref(loc, wref)


def _promote_checks(h: BackendHarness, loc: Locator, *, conditional: bool) -> None:
    """`promote`: the base follows the fork; a fast-forward is idempotent; a
    base that moved on its own is refused, and a stale `expected` is refused
    as `RefMovedError` before anything else is compared -- also where the
    fast-forward itself would land -- while the right one lets it land."""
    b = h.backend
    takes = _takes_expected(b, "promote", conditional)
    base = b.fingerprint(loc, None)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance-promote")
    wref = b.fork(loc, base, name)
    h.mutate(loc, wref)
    fork_head = b.fingerprint(loc, wref)
    if takes:
        # The fork's head is a state the base never held: a backend that
        # ignores `expected` fast-forwards here.
        _refuses(
            lambda: b.promote(loc, wref, expected=fork_head),
            RefMovedError,
            "promote with a stale expected must raise RefMovedError, also when "
            "the fast-forward would land",
        )
        assert _same(b, b.fingerprint(loc, None), base), (
            "a refused promote must leave the base where it was"
        )
        moved = b.promote(loc, wref, expected=base)
    else:
        moved = b.promote(loc, wref)
    assert _same(b, moved, b.fingerprint(loc, None)), (
        "promote must return the base's new state"
    )
    assert not _same(b, moved, base), "promote must move the base"
    assert b.ancestor_of(loc, base, moved) in (True, None), (
        "promote must keep the base's history"
    )
    if _same(b, moved, fork_head):
        # A fast-forward: the base *is* the fork's head, so promoting again
        # has nothing to do. (A system whose promotion is a merge commit is
        # not "already there" in this sense.)
        again = b.promote(loc, wref)
        assert _same(b, again, moved), (
            "a second promote is a no-op returning the same state"
        )
    # Diverge: the base moves on its own, and the fork too. A fast-forward
    # would discard the base's commit.
    h.mutate(loc, None)
    h.mutate(loc, wref)
    diverged = b.fingerprint(loc, None)
    _refuses(
        lambda: b.promote(loc, wref),
        BackendError,
        "promote onto a base that moved must raise BackendError",
    )
    assert _same(b, b.fingerprint(loc, None), diverged), (
        "a refused promote must leave the base where it was"
    )
    if takes:
        _refuses(
            lambda: b.promote(loc, wref, expected=moved),
            RefMovedError,
            "promote with a stale expected must raise RefMovedError",
        )
        assert _same(b, b.fingerprint(loc, None), diverged)
    b.delete_working_ref(loc, wref)


def _merge_checks(h: BackendHarness, loc: Locator, *, conditional: bool) -> None:
    """`merge`: two sides written to different places merge into a new base
    state; a stale `expected` is refused as `RefMovedError` -- also where
    the merge itself would land -- and the right one lets it land."""
    b = h.backend
    takes = _takes_expected(b, "merge", conditional)
    base = b.fingerprint(loc, None)
    name = working_ref_name(CONFORMANCE_DATASET, "conformance-merge")
    wref = b.fork(loc, base, name)
    h.mutate(loc, wref)
    fork_head = b.fingerprint(loc, wref)
    h.mutate(loc, None)
    before = b.fingerprint(loc, None)
    if takes:
        # Reviewed before the base's own write: a backend that ignores
        # `expected` merges here.
        _refuses(
            lambda: b.merge(loc, wref, "conformance merge", expected=base),
            RefMovedError,
            "merge with a stale expected must raise RefMovedError, also when the "
            "merge would land",
        )
        assert _same(b, b.fingerprint(loc, None), before), (
            "a refused merge must leave the base where it was"
        )
        merged = b.merge(loc, wref, "conformance merge", expected=before)
    else:
        merged = b.merge(loc, wref, "conformance merge")
    assert _same(b, merged, b.fingerprint(loc, None)), (
        "merge must return the base's new state"
    )
    assert not _same(b, merged, before) and not _same(b, merged, fork_head), (
        "merging two diverged sides makes a new state"
    )
    assert b.ancestor_of(loc, before, merged) in (True, None), (
        "merge must keep the base's history"
    )
    if takes:
        _refuses(
            lambda: b.merge(loc, wref, "conformance merge", expected=before),
            RefMovedError,
            "merge with a stale expected must raise RefMovedError",
        )
        assert _same(b, b.fingerprint(loc, None), merged), (
            "a refused merge must leave the base where it was"
        )
    b.delete_working_ref(loc, wref)


def run_conformance(harness: BackendHarness) -> None:
    """Run all capability-appropriate checks; raise ``AssertionError`` on fail.

    The suite uses ``harness.capabilities`` when provided (so a backend whose
    effective tier varies per object -- e.g. ``file`` -- can be exercised at a
    specific tier), otherwise the backend's class-level capabilities.
    """
    b = harness.backend
    caps = getattr(harness, "capabilities", None) or b.capabilities
    assert Capability.FINGERPRINT in caps, "every backend must fingerprint"

    loc = harness.new_object()
    before, state = _fingerprint_checks(harness, loc)
    _content_checks(harness, loc, before, state)
    _identity_checks(harness, loc, state)

    if Capability.DIFF in caps:
        _diff_checks(harness, loc, before, state)

    if Capability.HISTORY in caps:
        _history_checks(harness, loc, before, state)

    if Capability.ADDRESSABLE in caps:
        _addressable_checks(harness, loc, before, state)

    if Capability.PIN in caps:
        pid = _pin_checks(harness, loc, state)
        if Capability.FORK in caps:
            _fork_checks(harness, loc, state, pid)
        _unpin_checks(harness, loc, state, pid)

    if Capability.FORK in caps:
        # Declared per object where the store decides (Icechunk: object
        # stores only), so the harness's locator is asked too.
        conditional = Capability.CONDITIONAL_REF in (
            caps | effective_capabilities(b, loc, Policy())
        )
        _conditional_fork_checks(harness, loc, conditional=conditional)
        if conditional:
            _conditional_race_checks(harness, loc)
        if Capability.PROMOTE in caps or Capability.MERGE in caps:
            _ancestry_checks(harness, loc)
        if Capability.PROMOTE in caps:
            _promote_checks(harness, loc, conditional=conditional)
        if Capability.MERGE in caps:
            _merge_checks(harness, loc, conditional=conditional)

    if Capability.CREATE in caps:
        _create_checks(harness)
