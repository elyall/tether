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
        assert state == {"lsn": "0/16B3748", "next_xid": "742"}

        pin = backend.pin(LOCATOR, state, "abc123def456")
        assert pin.ref == ref_for_pin("abc123def456")
        assert "abc123def456" in backend.list_pins(LOCATOR)
        assert backend.verify(LOCATOR, state, pin, deep=False).ok

        # Fork a working branch and open it writable (creates an endpoint).
        wref = backend.fork(LOCATOR, pin, "tether.ws.abcd1234.db")
        handle = backend.open(LOCATOR, wref, read_only=False)
        assert isinstance(handle, NeonHandle)
        assert handle.url.startswith("postgresql://")
        assert any(e["type"] == "read_write" for e in fake.endpoints)

        # Working branches are excluded from list_pins.
        assert "ws.abcd1234.db" not in backend.list_pins(LOCATOR)

        backend.unpin(LOCATOR, pin)
        assert "abc123def456" not in backend.list_pins(LOCATOR)
        assert backend.verify(LOCATOR, state, pin, deep=False).status is (
            VerifyStatus.MISSING
        )


def test_verify_detects_lsn_drift(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        pin = backend.pin(LOCATOR, {"lsn": "0/16B3748"}, "aaaa1111bbbb")
        report = backend.verify(LOCATOR, {"lsn": "0/DIFFERENT"}, pin, deep=False)
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
