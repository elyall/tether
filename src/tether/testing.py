"""Importable, capability-parametrized conformance suite for backends.

A backend author implements a small :class:`BackendHarness` (create a fresh
object; mutate its current state) and calls :func:`run_conformance`. The suite
runs only the checks appropriate to the backend's declared capabilities, so the
same spec validates an Observed file backend and a Forkable Icechunk backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tether.backends.base import (
    Capability,
    ObjectBackend,
    ObjectDiff,
    VerifyStatus,
    content_state,
)
from tether.handles import Handle
from tether.manifest import (
    Locator,
    compute_pin_id,
    ref_for_pin,
    working_ref_name,
)

__all__ = ["BackendHarness", "run_conformance"]


@runtime_checkable
class BackendHarness(Protocol):
    """Test fixture a backend author provides to drive the suite."""

    backend: ObjectBackend

    def new_object(self) -> Locator:
        """Create a fresh, empty system and return a locator for it."""

    def mutate(self, locator: Locator, working_ref: str | None) -> None:
        """Cause the state at ``working_ref`` (or the base) to change."""


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
    assert b.fingerprint(loc, None) == s1, "fingerprint must be stable"
    h.mutate(loc, None)
    s2 = b.fingerprint(loc, None)
    assert s2 != s1, "fingerprint must change after a mutation"
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
    pid1 = compute_pin_id(b.kind, ident, content)
    pid2 = compute_pin_id(b.kind, b.identity(loc), content)
    assert pid1 == pid2, "pin id must be deterministic"


def _pin_checks(h: BackendHarness, loc: Locator, state: dict) -> str:
    b = h.backend
    # Pin ids hash the content state (what the engine does); pin() gets the
    # full state, volatile address keys included.
    content = content_state(b, state)
    assert content is not None
    pid = compute_pin_id(b.kind, b.identity(loc), content)
    pin = b.pin(loc, state, pid)
    assert pin.ref.startswith(ref_for_pin("")), "pin ref must carry the prefix"
    assert pid in b.list_pins(loc), "list_pins must include a fresh pin"
    assert b.verify(loc, state, pin, deep=False).ok, "fresh pin must verify"
    assert b.verify(loc, state, pin, deep=True).ok, "deep verify must pass"
    # Idempotent: re-pinning identical state returns the same ref.
    assert b.pin(loc, state, pid).ref == pin.ref, "pin must be idempotent"
    # The pin protects state against later drift of the working ref.
    h.mutate(loc, None)
    assert b.verify(loc, state, pin, deep=False).ok, "pin must protect state"
    return pid


def _fork_checks(h: BackendHarness, loc: Locator, state: dict, pid: str) -> None:
    b = h.backend
    pin = b.pin(loc, state, pid)
    name = working_ref_name("ws012345", "conformance/obj")
    wref = b.fork(loc, pin, name)
    assert isinstance(wref, str) and wref, "fork must return a working ref"
    forked = b.fingerprint(loc, wref)
    assert forked == state, "a fresh fork must start at the pinned state"
    handle = b.open(loc, wref, read_only=False)
    assert isinstance(handle, Handle) and not handle.read_only
    # Writing to the fork changes only the fork.
    base_before = b.fingerprint(loc, None)
    h.mutate(loc, wref)
    assert b.fingerprint(loc, wref) != forked, "writes to a fork must register"
    assert b.fingerprint(loc, None) == base_before, "fork must isolate the base"
    # Reset contract: forking onto an existing name moves it back to the source.
    again = b.fork(loc, pin, name)
    assert b.fingerprint(loc, again) == state, (
        "fork onto an existing name must reset the branch to the source"
    )
    wref = again
    listed = b.list_working_refs(loc)
    assert wref in listed, f"list_working_refs must include {wref!r} (got {listed})"
    b.delete_working_ref(loc, wref)
    assert wref not in b.list_working_refs(loc), "deleted working ref still listed"
    # Pin-less fork: straight from the recorded state (policy.pin = "record").
    name2 = working_ref_name("ws012345", "conformance/pinless")
    wref2 = b.fork(loc, state, name2)
    assert b.fingerprint(loc, wref2) == state, "fork from state must start there"
    b.delete_working_ref(loc, wref2)


def _addressable_checks(h: BackendHarness, loc: Locator, state: dict) -> None:
    b = h.backend
    assert b.verify(loc, state, None, deep=False).status in (
        VerifyStatus.OK,
        VerifyStatus.UNKNOWN,
    ), "addressable state should verify (or be unknown without --deep)"
    handle = b.open(loc, state, read_only=True)
    assert isinstance(handle, Handle) and handle.read_only


def _unpin_checks(h: BackendHarness, loc: Locator, state: dict, pid: str) -> None:
    b = h.backend
    pin = b.pin(loc, state, pid)
    ro = b.open(loc, pin, read_only=True)
    assert isinstance(ro, Handle) and ro.read_only
    b.unpin(loc, pin)
    assert pid not in b.list_pins(loc), "unpin must drop the pin id"
    assert b.verify(loc, state, pin, deep=False).status is VerifyStatus.MISSING


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
        _addressable_checks(harness, loc, state)

    if Capability.PIN in caps:
        pid = _pin_checks(harness, loc, state)
        if Capability.FORK in caps:
            _fork_checks(harness, loc, state, pid)
        _unpin_checks(harness, loc, state, pid)
