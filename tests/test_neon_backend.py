from __future__ import annotations

import re

import pytest

respx = pytest.importorskip("respx")
import httpx  # noqa: E402

from tether.backends.base import VerifyStatus  # noqa: E402
from tether.backends.neon import NeonBackend  # noqa: E402
from tether.errors import BackendError  # noqa: E402
from tether.handles import NeonHandle  # noqa: E402
from tether.manifest import ref_for_pin  # noqa: E402

BASE = "http://neon.test/api/v2"
PID = "p1"
LOCATOR = {"project_id": PID, "database": "neondb", "role": "runner", "branch": "main"}


class FakeNeon:
    """A tiny stateful fake of the Neon control plane for respx."""

    def __init__(self) -> None:
        self.branches: dict[str, dict] = {
            "br-main": {
                "id": "br-main",
                "name": "main",
                "parent_id": None,
                "parent_lsn": None,
                "protected": False,
                "last_reset_at": None,
            }
        }
        self.endpoints: list[dict] = []
        self.restores: list[tuple[str, dict]] = []
        self._n = 0

    def install(self, router: respx.MockRouter) -> None:
        router.get(url__regex=rf"{re.escape(BASE)}/projects/{PID}/branches$").mock(
            side_effect=self._list_branches
        )
        router.post(url__regex=rf"{re.escape(BASE)}/projects/{PID}/branches$").mock(
            side_effect=self._create_branch
        )
        router.delete(
            url__regex=rf"{re.escape(BASE)}/projects/{PID}/branches/[^/]+$"
        ).mock(side_effect=self._delete_branch)
        router.post(
            url__regex=rf"{re.escape(BASE)}/projects/{PID}/branches/[^/]+/restore$"
        ).mock(side_effect=self._restore_branch)
        router.get(url__regex=rf"{re.escape(BASE)}/projects/{PID}/endpoints$").mock(
            side_effect=self._list_endpoints
        )
        router.post(url__regex=rf"{re.escape(BASE)}/projects/{PID}/endpoints$").mock(
            side_effect=self._create_endpoint
        )
        router.get(
            url__regex=rf"{re.escape(BASE)}/projects/{PID}/connection_uri.*"
        ).mock(side_effect=self._connection_uri)

    # handlers
    def _list_branches(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"branches": list(self.branches.values())})

    def _create_branch(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)["branch"]
        self._n += 1
        bid = f"br-{self._n}"
        self.branches[bid] = {
            "id": bid,
            "name": body["name"],
            "parent_id": body.get("parent_id"),
            "parent_lsn": body.get("parent_lsn"),
            "protected": body.get("protected", False),
            "last_reset_at": None,
        }
        return httpx.Response(201, json={"branch": self.branches[bid]})

    def _delete_branch(self, request: httpx.Request) -> httpx.Response:
        bid = request.url.path.rsplit("/", 1)[-1]
        self.branches.pop(bid, None)
        return httpx.Response(200, json={})

    def _restore_branch(self, request: httpx.Request) -> httpx.Response:
        import json

        bid = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        br = self.branches[bid]
        br["parent_id"] = body["source_branch_id"]
        br["parent_lsn"] = body.get("source_lsn")
        br["last_reset_at"] = "2026-09-08T00:00:00Z"
        self.restores.append((bid, body))
        return httpx.Response(200, json={"branch": br})

    def _list_endpoints(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"endpoints": self.endpoints})

    def _create_endpoint(self, request: httpx.Request) -> httpx.Response:
        import json

        ep = json.loads(request.content)["endpoint"]
        self._n += 1
        record = {
            "id": f"ep-{self._n}",
            "branch_id": ep["branch_id"],
            "type": ep["type"],
        }
        self.endpoints.append(record)
        return httpx.Response(201, json={"endpoint": record})

    def _connection_uri(self, request: httpx.Request) -> httpx.Response:
        bid = request.url.params.get("branch_id")
        return httpx.Response(200, json={"uri": f"postgresql://neon/{bid}"})


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> NeonBackend:
    monkeypatch.setenv("NEON_API_KEY", "secret")
    b = NeonBackend({"api_url": BASE})
    monkeypatch.setattr(b, "_probe", lambda uri: ("0/16B3748", "742"))
    return b


def test_pin_fork_verify_unpin(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)

        state = backend.fingerprint(LOCATOR, None)
        assert state == {"lsn": "0/16B3748", "next_xid": "742", "branch": "main"}

        pin = backend.pin(LOCATOR, state, "abc123def456")
        assert pin.ref == ref_for_pin("abc123def456")
        assert "abc123def456" in backend.list_pins(LOCATOR)
        assert backend.verify(LOCATOR, state, pin, deep=False).ok
        pin_br = next(b for b in fake.branches.values() if b["name"] == pin.ref)
        assert pin_br["parent_id"] == "br-main" and pin_br["protected"] is True

        # Fork a working branch and open it writable (creates an endpoint).
        wref = backend.fork(LOCATOR, pin, "tether.ws.abcd1234.db")
        handle = backend.open(LOCATOR, wref, read_only=False)
        assert isinstance(handle, NeonHandle)
        assert handle.url.startswith("postgresql://")
        assert any(e["type"] == "read_write" for e in fake.endpoints)
        assert backend.list_working_refs(LOCATOR) == [wref]

        # Working branches are excluded from list_pins.
        assert "ws.abcd1234.db" not in backend.list_pins(LOCATOR)

        backend.unpin(LOCATOR, pin)
        assert "abc123def456" not in backend.list_pins(LOCATOR)
        assert backend.verify(LOCATOR, state, pin, deep=False).status is (
            VerifyStatus.MISSING
        )


def test_pins_hang_off_the_branch_the_state_came_from(
    backend: NeonBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork's LSN lives on the fork's timeline, so its pin is a child of the fork."""
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        base = backend.fingerprint(LOCATOR, None)
        base_pin = backend.pin(LOCATOR, base, "000000000001")
        wref = backend.fork(LOCATOR, base_pin, "tether.ws.abcd1234.db")
        work_br = next(b for b in fake.branches.values() if b["name"] == wref)

        # Writes on the fork move its LSN; the state says which branch that is.
        monkeypatch.setattr(backend, "_probe", lambda uri: ("0/2000000", "900"))
        forked = backend.fingerprint(LOCATOR, wref)
        assert forked == {"lsn": "0/2000000", "next_xid": "900", "branch": wref}

        pin = backend.pin(LOCATOR, forked, "000000000002")
        pin_br = next(b for b in fake.branches.values() if b["name"] == pin.ref)
        assert pin_br["parent_id"] == work_br["id"]  # not br-main
        assert pin_br["parent_lsn"] == "0/2000000"
        assert backend.verify(LOCATOR, forked, pin, deep=False).ok

        # A pin created under the wrong parent is reported as drift.
        pin_br["parent_id"] = "br-main"
        report = backend.verify(LOCATOR, forked, pin, deep=False)
        assert report.status is VerifyStatus.DRIFTED and "hangs off" in report.message

        # Pin-less fork and time-travel open also use the state's branch.
        other = backend.fork(LOCATOR, forked, "tether.ws.ffff9999.db")
        other_br = next(b for b in fake.branches.values() if b["name"] == other)
        assert other_br["parent_id"] == work_br["id"]
        assert other_br["parent_lsn"] == "0/2000000"
        ro = backend.open(LOCATOR, forked, read_only=True)
        assert isinstance(ro, NeonHandle) and ro.branch == wref
        assert "neon_lsn:0/2000000" in ro.url


def test_verify_detects_lsn_drift(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        pin = backend.pin(
            LOCATOR, {"lsn": "0/16B3748", "branch": "main"}, "aaaa1111bbbb"
        )
        report = backend.verify(
            LOCATOR, {"lsn": "0/DIFFERENT", "branch": "main"}, pin, deep=False
        )
        assert report.status is VerifyStatus.DRIFTED


def test_quiescence_check(
    backend: NeonBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        monkeypatch.setattr(backend, "_active_writers", lambda uri: 3)
        with pytest.raises(BackendError):
            backend.check_quiescence(LOCATOR, None)
        monkeypatch.setattr(backend, "_active_writers", lambda uri: 0)
        backend.check_quiescence(LOCATOR, None)  # no raise


def test_lsn_motion_without_writes_is_not_a_change(
    backend: NeonBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoints move the LSN; only next_xid says the data changed."""
    from tether.backends.base import content_state
    from tether.manifest import compute_pin_id

    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        before = backend.fingerprint(LOCATOR, None)
        monkeypatch.setattr(backend, "_probe", lambda uri: ("0/16B4000", "742"))
        after = backend.fingerprint(LOCATOR, None)
        assert before != after  # the address moved ...
        assert content_state(backend, before) == content_state(
            backend, after
        )  # ... not the data
        ident = backend.identity(LOCATOR)
        c_before, c_after = (
            content_state(backend, before),
            content_state(backend, after),
        )
        assert c_before is not None and c_after is not None
        assert compute_pin_id("neon", ident, c_before) == compute_pin_id(
            "neon", ident, c_after
        )
        monkeypatch.setattr(backend, "_probe", lambda uri: ("0/16B5000", "743"))
        moved = backend.fingerprint(LOCATOR, None)
        assert content_state(backend, before) != content_state(backend, moved)


def test_fork_onto_an_existing_branch_restores_it(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        pin1 = backend.pin(LOCATOR, state, "000000000001")
        wref = backend.fork(LOCATOR, pin1, "tether.ws.abcd1234.db")
        work = next(b for b in fake.branches.values() if b["name"] == wref)
        assert fake.restores == []
        assert backend.fork(LOCATOR, pin1, wref) == wref  # same source: no-op
        assert fake.restores == []

        # Re-forking from a different pin resets the existing branch onto it.
        pin2 = backend.pin(LOCATOR, {**state, "lsn": "0/2000000"}, "000000000002")
        assert backend.fork(LOCATOR, pin2, wref) == wref
        pin2_br = next(b for b in fake.branches.values() if b["name"] == pin2.ref)
        assert fake.restores and fake.restores[-1][0] == work["id"]
        assert work["parent_id"] == pin2_br["id"]

        # Pin-less fork from a state restores at that LSN.
        assert backend.fork(LOCATOR, {**state, "lsn": "0/3000000"}, wref) == wref
        assert work["parent_lsn"] == "0/3000000"

        # An existing pin branch that points elsewhere is refused, not reused.
        with pytest.raises(BackendError, match="hangs off"):
            backend.pin(LOCATOR, {**state, "lsn": "0/9999999"}, "000000000001")
