from __future__ import annotations

import re
from pathlib import Path

import pytest

respx = pytest.importorskip("respx")
import httpx  # noqa: E402

from tether.backends.base import (  # noqa: E402
    ObjectBackend,
    VerifyStatus,
    content_state,
)
from tether.errors import BackendError  # noqa: E402
from tether.experimental.backends.neon import NeonBackend  # noqa: E402
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
        # The project's default branch comes with its primary compute.
        self.endpoints: list[dict] = [
            {"id": "ep-main", "branch_id": "br-main", "type": "read_write"}
        ]
        self.listings = 0  # how often the branch list was fetched
        self.restores: list[tuple[str, dict]] = []
        self.uris: list[dict[str, str]] = []  # connection_uri query params
        self.busy: list[int] = []  # statuses the next requests get first
        self._n = 0

    def install(self, router: respx.MockRouter) -> None:
        def route(method: str, path: str, handler) -> None:
            getattr(router, method)(
                url__regex=rf"{re.escape(BASE)}/projects/{PID}/{path}"
            ).mock(side_effect=self._maybe_busy(handler))

        route("get", r"branches(\?.*)?$", self._list_branches)
        route("post", "branches$", self._create_branch)
        route("delete", "branches/[^/]+$", self._delete_branch)
        route("post", "branches/[^/]+/restore$", self._restore_branch)
        route("patch", "branches/[^/]+$", self._patch_branch)
        route("get", "endpoints$", self._list_endpoints)
        route("post", "endpoints$", self._create_endpoint)
        route("get", "connection_uri.*", self._connection_uri)

    def _maybe_busy(self, handler):
        def respond(request: httpx.Request) -> httpx.Response:
            if self.busy:
                return httpx.Response(self.busy.pop(0), json={"message": "busy"})
            return handler(request)

        return respond

    # handlers
    PAGE = 2  # small pages, so every listing in the tests exercises pagination

    def _list_branches(self, request: httpx.Request) -> httpx.Response:
        self.listings += 1
        # The real API pages: `cursor` names the last item seen, the reply
        # carries `pagination.cursor` while more follow.
        items = list(self.branches.values())
        cursor = request.url.params.get("cursor")
        start = 0
        if cursor:
            ids = [b["id"] for b in items]
            start = ids.index(cursor) + 1 if cursor in ids else len(items)
        page = items[start : start + self.PAGE]
        body: dict = {"branches": page}
        if start + self.PAGE < len(items) and page:
            body["pagination"] = {"cursor": page[-1]["id"]}
        return httpx.Response(200, json=body)

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
        if any(b.get("parent_id") == bid for b in self.branches.values()):
            return httpx.Response(
                409, json={"message": "branch has children; delete them first"}
            )
        if self.branches.get(bid, {}).get("protected"):
            return httpx.Response(
                422, json={"message": "protected branches cannot be deleted"}
            )
        self.branches.pop(bid, None)
        return httpx.Response(200, json={})

    def _patch_branch(self, request: httpx.Request) -> httpx.Response:
        import json

        bid = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)["branch"]
        br = self.branches[bid]
        if "name" in body:
            br["name"] = body["name"]
        if "protected" in body:
            br["protected"] = bool(body["protected"])
        return httpx.Response(200, json={"branch": br})

    def _restore_branch(self, request: httpx.Request) -> httpx.Response:
        import json

        bid = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        br = self.branches[bid]
        # Neon refuses to restore a branch that has children unless the old
        # state is preserved under a new name (children move onto it).
        children = [b for b in self.branches.values() if b.get("parent_id") == bid]
        if children and not body.get("preserve_under_name"):
            return httpx.Response(
                400,
                json={"message": "branch has children; preserve_under_name required"},
            )
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
        # As the real API: database and role are required, and without an
        # `endpoint_id` the URI is the branch's read-write compute's.
        params = dict(request.url.params)
        self.uris.append(params)
        bid = params.get("branch_id")
        db, role = params.get("database_name"), params.get("role_name")
        if not db or not role:
            return httpx.Response(400, json={"message": "database_name, role_name"})
        mine = [e for e in self.endpoints if e["branch_id"] == bid]
        if "endpoint_id" in params:
            ok = any(e["id"] == params["endpoint_id"] for e in mine)
        else:
            ok = any(e["type"] == "read_write" for e in mine)
        if not ok:
            return httpx.Response(400, json={"message": "no such endpoint"})
        return httpx.Response(200, json={"uri": f"postgresql://neon/{bid}?dbname={db}"})


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
        assert state == {"lsn": "0/16B3748", "commit_xid": "742", "branch": "main"}

        pin = backend.pin(LOCATOR, state, "abc123def456")
        assert pin.ref == ref_for_pin("abc123def456")
        assert "abc123def456" in backend.list_pins(LOCATOR)
        assert backend.verify(LOCATOR, state, pin, deep=False).ok
        pin_br = next(b for b in fake.branches.values() if b["name"] == pin.ref)
        assert pin_br["parent_id"] == "br-main" and pin_br["protected"] is False

        # Fork a working branch and open it writable (creates an endpoint).
        wref = backend.fork(LOCATOR, pin, "tether.ws.abcd1234.db")
        handle = backend.open(LOCATOR, wref, read_only=False)
        assert isinstance(handle, NeonHandle)
        assert handle.url.startswith("postgresql://")
        assert any(e["type"] == "read_write" for e in fake.endpoints)
        assert backend.list_working_refs(LOCATOR) == [wref]

        # Working branches are excluded from list_pins.
        assert "ws.abcd1234.db" not in backend.list_pins(LOCATOR)

        # The fork is a child of the pin branch: Neon will not delete the pin
        # while it exists (the pin is the fork's storage).
        with pytest.raises(BackendError):
            backend.unpin(LOCATOR, pin)
        backend.delete_working_ref(LOCATOR, wref)
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

        # Untouched, the fork reports its parent's lineage and the same xid:
        # the same *content* as the pin, on its own timeline.
        untouched = backend.fingerprint(LOCATOR, wref)
        assert untouched == {
            "lsn": "0/16B3748",
            "commit_xid": "742",
            "branch": "main",
            "timeline": wref,
        }
        assert content_state(backend, untouched) == content_state(backend, base)

        # Writes on the fork move its xid; the state says which timeline that is.
        monkeypatch.setattr(backend, "_probe", lambda uri: ("0/2000000", "900"))
        forked = backend.fingerprint(LOCATOR, wref)
        assert forked == {
            "lsn": "0/2000000",
            "commit_xid": "900",
            "branch": "main",
            "timeline": wref,
        }

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


def test_neon_protects_pins_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pin is a branch. Neon's Free plan has no protected branches and
    paid plans allow a handful, so protected pins failed on the first commit,
    or the third or sixth: unprotected is the default, `protected_pins = true`
    the opt-in."""
    monkeypatch.setenv("NEON_API_KEY", "secret")
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        for config, protected in (({}, False), ({"protected_pins": True}, True)):
            b = NeonBackend({"api_url": BASE, **config})
            monkeypatch.setattr(b, "_probe", lambda uri: ("0/16B3748", "742"))
            pin = b.pin(LOCATOR, b.fingerprint(LOCATOR, None), "abc123def456")
            br = next(x for x in fake.branches.values() if x["name"] == pin.ref)
            assert br["protected"] is protected
            b.unpin(LOCATOR, pin)  # a protected pin is unprotected first
            assert pin.ref not in {x["name"] for x in fake.branches.values()}


def test_neon_handle_keeps_the_password_out_of_repr() -> None:
    h = NeonHandle(
        key="main", read_only=False, url="postgresql://u:pw@h/db", branch="main"
    )
    assert "pw" not in repr(h)
    assert h.redacted_url == "postgresql://u:***@h/db"
    assert h.url == "postgresql://u:pw@h/db"  # what psql needs


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
    """Checkpoints move the LSN; only commit_xid says the data changed."""
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
        assert compute_pin_id("neon", ident, c_before, "d5d5d5d5") == compute_pin_id(
            "neon", ident, c_after, "d5d5d5d5"
        )
        monkeypatch.setattr(backend, "_probe", lambda uri: ("0/16B5000", "743"))
        moved = backend.fingerprint(LOCATOR, None)
        assert content_state(backend, before) != content_state(backend, moved)


def test_two_databases_of_one_project_share_pins_and_branches(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Neon branch at an LSN is a snapshot of the whole project, so two
    objects on two databases of one project are one snapshot to tether: one
    pin branch per commit, one working branch per bookmark, and `open` on
    each connects to its own database through them."""
    from tether.repo import Repo

    monkeypatch.setenv("NEON_API_KEY", "secret")
    fake = FakeNeon()
    probe = _Probe(fake)
    with respx.mock as router:
        fake.install(router)
        repo = Repo.init(vcs_root)
        (vcs_root / ".tether" / "secrets.toml").write_text(
            f'[backends.neon]\napi_url = "{BASE}"\n'
        )
        (vcs_root / ".tether" / "secrets.toml").chmod(0o600)
        repo = Repo.find(vcs_root)
        backend = repo.backend_for("neon")
        monkeypatch.setattr(backend, "_probe", probe)
        monkeypatch.setattr(backend, "_active_writers", lambda uri: 0)
        assert backend.identity(LOCATOR) == backend.identity(
            {**LOCATOR, "database": "analytics", "role": "reader"}
        )
        repo.add("app", "neon", dict(LOCATOR))
        repo.add("analytics", "neon", {**LOCATOR, "database": "analytics"})

        pins_before = len(backend.list_pins(LOCATOR))
        res = repo.commit("baseline")
        assert res.pinned["app"] is not None and res.pinned["analytics"] is not None
        assert res.pinned["app"].id == res.pinned["analytics"].id
        assert len(backend.list_pins(LOCATOR)) == pins_before + 1  # one branch

        repo.new(bookmark="work", eager=True)
        refs = repo.workspace.working_refs
        assert refs["app"] == refs["analytics"]  # one fork, the other shares
        assert sum(1 for b in fake.branches.values() if b["name"] == refs["app"]) == 1
        app = repo.open("app", read_only=False)
        analytics = repo.open("analytics", read_only=False)
        assert isinstance(app, NeonHandle) and isinstance(analytics, NeonHandle)
        assert app.url.endswith("?dbname=neondb")
        assert analytics.url.endswith("?dbname=analytics")
        assert app.branch == analytics.branch == refs["app"]


def test_fork_onto_an_existing_branch_restores_it(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        pin1 = backend.pin(LOCATOR, state, "000000000001")
        wref = backend.fork(LOCATOR, pin1, "tether.ws.abcd1234.db")
        work = next(b for b in fake.branches.values() if b["name"] == wref)
        pin1_br = next(b for b in fake.branches.values() if b["name"] == pin1.ref)
        assert fake.restores == []

        # Same source again: parent_id still says pin1, but the head may have
        # moved (writes, never committed), so the branch is restored anyway.
        assert backend.fork(LOCATOR, pin1, wref) == wref
        assert fake.restores == [(work["id"], {"source_branch_id": pin1_br["id"]})]

        # Re-forking from a different pin resets the existing branch onto it.
        pin2 = backend.pin(LOCATOR, {**state, "lsn": "0/2000000"}, "000000000002")
        assert backend.fork(LOCATOR, pin2, wref) == wref
        pin2_br = next(b for b in fake.branches.values() if b["name"] == pin2.ref)
        assert len(fake.restores) == 2 and fake.restores[-1][0] == work["id"]
        assert work["parent_id"] == pin2_br["id"]

        # Pin-less fork from a state restores at that LSN.
        assert backend.fork(LOCATOR, {**state, "lsn": "0/3000000"}, wref) == wref
        assert work["parent_lsn"] == "0/3000000"
        assert len(fake.restores) == 3

        # Once a pin hangs off the working branch (a commit made there), Neon
        # cannot restore it in place. The pin keeps its parent; the fork lands
        # on a sibling name, which is what the engine records.
        on_work = {"lsn": "0/4000000", "commit_xid": "900", "branch": wref}
        pin3 = backend.pin(LOCATOR, on_work, "000000000003")
        pin3_br = next(b for b in fake.branches.values() if b["name"] == pin3.ref)
        assert pin3_br["parent_id"] == work["id"]
        sibling = backend.fork(LOCATOR, pin1, wref)
        assert sibling == f"{wref}.2"
        assert len(fake.restores) == 3  # untouched
        sib = next(b for b in fake.branches.values() if b["name"] == sibling)
        assert sib["parent_id"] == pin1_br["id"]
        assert pin3_br["parent_id"] == work["id"]  # pin still hangs off its branch
        assert backend.fork(LOCATOR, pin1, wref) == f"{wref}.3"  # .2 is taken
        assert set(backend.list_working_refs(LOCATOR)) >= {
            wref,
            f"{wref}.2",
            f"{wref}.3",
        }

        # An existing pin branch that points elsewhere is refused, not reused.
        with pytest.raises(BackendError, match="hangs off"):
            backend.pin(LOCATOR, {**state, "lsn": "0/9999999"}, "000000000001")


def test_rename_pin_and_branch_in_place(backend: NeonBackend) -> None:
    """Pins are branches with children; renames must not delete and recreate."""
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        old = backend.pin(LOCATOR, state, "abcdef012345")
        wref = backend.fork(LOCATOR, old, "tether.ws.7c1e0a4d.db")
        pin_br = next(b for b in fake.branches.values() if b["name"] == old.ref)
        work_br = next(b for b in fake.branches.values() if b["name"] == wref)
        assert work_br["parent_id"] == pin_br["id"]  # the pin has a child

        new = backend.rename_pin(LOCATOR, old, state, "d5d5d5d5.0123456789abcdef")
        assert new.ref == "tether.d5d5d5d5.0123456789abcdef"
        assert (
            pin_br["name"] == new.ref and pin_br["id"] in fake.branches
        )  # same branch
        assert work_br["parent_id"] == pin_br["id"]  # child untouched
        assert old.ref not in {b["name"] for b in fake.branches.values()}

        renamed = backend.rename_working_ref(
            LOCATOR, wref, "tether.ws.d5d5d5d5.7c1e0a4d.db-61a22c"
        )
        assert renamed == work_br["name"] == "tether.ws.d5d5d5d5.7c1e0a4d.db-61a22c"
        assert fake.restores == []  # nothing was reset along the way


def test_working_ref_blockers_names_the_pin_children(backend: NeonBackend) -> None:
    fake = FakeNeon()
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        pin = backend.pin(LOCATOR, state, "d5d5d5d5.0000000000000001")
        wref = backend.fork(LOCATOR, pin, "tether.ws.d5d5d5d5.7c1e0a4d.db-61a22c")
        assert backend.working_ref_blockers(LOCATOR, wref) is None
        # A commit on the working branch hangs a pin off it.
        child = backend.pin(
            LOCATOR,
            {"lsn": "0/4000000", "commit_xid": "900", "branch": wref},
            "d5d5d5d5.0000000000000002",
        )
        why = backend.working_ref_blockers(LOCATOR, wref)
        assert why is not None and child.ref in why and "release those pins" in why
        with pytest.raises(BackendError):
            backend.delete_working_ref(LOCATOR, wref)  # the fake enforces it too
        backend.unpin(LOCATOR, child)
        assert backend.working_ref_blockers(LOCATOR, wref) is None
        backend.delete_working_ref(LOCATOR, wref)
        assert wref not in backend.list_working_refs(LOCATOR)


def test_locator_needs_database_and_role(vcs_root: Path) -> None:
    """The API builds no connection URI without both; refuse at `add`."""
    from tether.repo import Repo

    b = NeonBackend({"api_url": BASE})
    b.validate_locator(LOCATOR)
    for missing in ("project_id", "database", "role"):
        loc = {k: v for k, v in LOCATOR.items() if k != missing}
        with pytest.raises(BackendError, match=missing):
            b.validate_locator(loc)
    repo = Repo.init(vcs_root)
    with pytest.raises(BackendError, match="role"):
        repo.add("db", "neon", {"project_id": PID, "database": "neondb"})


def test_connections_name_the_endpoint_they_ensured(
    backend: NeonBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins get a read-only compute and forks none until used; a connection
    URI without `endpoint_id` means the branch's read-write compute."""
    fake = FakeNeon()
    monkeypatch.setattr(backend, "_active_writers", lambda uri: 0)
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        pin = backend.pin(LOCATOR, state, "abc123def456")
        pin_br = next(b for b in fake.branches.values() if b["name"] == pin.ref)
        ro = backend.open(LOCATOR, pin, read_only=True)
        assert isinstance(ro, NeonHandle) and ro.branch == pin.ref
        (pin_ep,) = [e for e in fake.endpoints if e["branch_id"] == pin_br["id"]]
        assert pin_ep["type"] == "read_only"
        assert fake.uris[-1]["endpoint_id"] == pin_ep["id"]

        # A fresh fork has no compute: fingerprinting it (eager `new`,
        # `status --snapshot`) makes its read-write endpoint first.
        wref = backend.fork(LOCATOR, pin, "tether.ws.abcd1234.db")
        work_br = next(b for b in fake.branches.values() if b["name"] == wref)
        assert backend.fingerprint(LOCATOR, wref)["branch"] == "main"
        (work_ep,) = [e for e in fake.endpoints if e["branch_id"] == work_br["id"]]
        assert work_ep["type"] == "read_write"
        assert fake.uris[-1]["endpoint_id"] == work_ep["id"]
        backend.check_quiescence(LOCATOR, wref)
        rw = backend.open(LOCATOR, wref, read_only=False)
        assert isinstance(rw, NeonHandle) and rw.branch == wref
        assert len(fake.endpoints) == 3  # reused, not created again
        assert all(u.get("endpoint_id") for u in fake.uris)


def test_api_retries_locked_and_throttled_responses(
    backend: NeonBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neon answers 423 while another operation on the project runs, 429 when
    throttled, and 503 when briefly unavailable; all are safe to retry."""
    fake = FakeNeon()
    waits: list[float] = []
    monkeypatch.setattr("tether.experimental.backends.neon.time.sleep", waits.append)
    with respx.mock as router:
        fake.install(router)
        state = backend.fingerprint(LOCATOR, None)
        fake.busy = [423, 429, 503]
        pin = backend.pin(LOCATOR, state, "abc123def456")
        assert pin.ref in {b["name"] for b in fake.branches.values()}
        assert len(waits) == 3 and waits == sorted(waits)

        fake.busy = [423] * 50
        with pytest.raises(BackendError, match="423"):
            backend.unpin(LOCATOR, pin)


def test_probe_ignores_xids_that_never_committed(pg_dsn: str) -> None:
    """A Neon compute starting from a basebackup inherits a `nextXid` rounded
    up past xids nobody used, so a restart moves it with no write. Those xids
    never commit; rolled-back ones stand in for them here."""
    psycopg = pytest.importorskip("psycopg")
    b = NeonBackend({})
    _, before = b._probe(pg_dsn)
    with psycopg.connect(pg_dsn) as conn:
        for _ in range(5):
            with conn.transaction(force_rollback=True):
                conn.execute("SELECT txid_current()")
    assert b._probe(pg_dsn)[1] == before
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE t (a int)")
    assert b._probe(pg_dsn)[1] != before


# --------------------------------------------------------------------------- #
# Driven through the shared suite and the engine, against the fake
# --------------------------------------------------------------------------- #
class _Probe:
    """A per-branch xid/LSN model behind `_probe`: a new branch inherits its
    parent's counters at creation (its content is the parent's), writes move
    only the branch written to, and LSNs move without writes too."""

    def __init__(self, fake: FakeNeon) -> None:
        self.fake = fake
        self.xid: dict[str, int] = {"br-main": 742}
        self.lsn: dict[str, int] = {"br-main": 0x16B3748}
        # (lsn, xid) as each branch advanced: a child cut at `parent_lsn`
        # inherits the parent's xid *at that LSN*, not its current one.
        self.history: dict[str, list[tuple[int, int]]] = {"br-main": [(0x16B3748, 742)]}
        self.cut: dict[str, tuple[object, ...]] = {"br-main": (None, None, None)}

    def _inherit(self, bid: str) -> None:
        meta = self.fake.branches[bid]
        cut = (meta.get("parent_id"), meta.get("parent_lsn"), meta.get("last_reset_at"))
        if bid in self.xid and self.cut.get(bid) == cut:
            return
        # First sight, or the branch was restored onto another cut point: its
        # content is the source's at that point again.
        self.cut[bid] = cut
        parent = meta.get("parent_id") or "br-main"
        self._inherit(parent)
        at = meta.get("parent_lsn")
        if at:
            point = int(str(at).split("/")[1], 16)
            xid = max(
                (x for lsn, x in self.history[parent] if lsn <= point), default=None
            )
            assert xid is not None, f"{bid} cut at an LSN {parent} never had"
            self.xid[bid], self.lsn[bid] = xid, point
        else:
            self.xid[bid], self.lsn[bid] = self.xid[parent], self.lsn[parent]
        self.history[bid] = [(self.lsn[bid], self.xid[bid])]

    def _advance(self, bid: str, lsn_by: int, xid_by: int) -> None:
        self._inherit(bid)
        self.lsn[bid] += lsn_by
        self.xid[bid] += xid_by
        self.history[bid].append((self.lsn[bid], self.xid[bid]))

    def __call__(self, uri: str) -> tuple[str, str]:
        bid = uri.split("?", 1)[0].rsplit("/", 1)[-1]
        self._advance(bid, 0x100, 0)  # a checkpoint: LSN moves, content does not
        return f"0/{self.lsn[bid]:X}", str(self.xid[bid])

    def write(self, name: str) -> None:
        bid = next(b["id"] for b in self.fake.branches.values() if b["name"] == name)
        self._advance(bid, 0x1000, 1)


class NeonHarness:
    def __init__(self, backend: NeonBackend, probe: _Probe) -> None:
        self.backend: ObjectBackend = backend
        self.probe = probe

    def new_object(self) -> dict:
        return dict(LOCATOR)

    def mutate(self, locator: dict, working_ref: str | None) -> None:
        self.probe.write(working_ref or "main")


def test_neon_conformance(monkeypatch: pytest.MonkeyPatch) -> None:
    from tether.testing import run_conformance

    monkeypatch.setenv("NEON_API_KEY", "secret")
    fake = FakeNeon()
    backend = NeonBackend({"api_url": BASE})
    probe = _Probe(fake)
    monkeypatch.setattr(backend, "_probe", probe)
    monkeypatch.setattr(backend, "_active_writers", lambda uri: 0)
    with respx.mock as router:
        fake.install(router)
        run_conformance(NeonHarness(backend, probe))


def test_neon_repo_lifecycle(vcs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """new -> open -> commit -> new again -> gc, the way a user drives it. An
    untouched fork is clean, a fork with writes pins once, and `new` on the
    bookmark after a commit reuses the branch without --discard."""
    from tether.repo import Repo

    monkeypatch.setenv("NEON_API_KEY", "secret")
    fake = FakeNeon()
    probe = _Probe(fake)
    with respx.mock as router:
        fake.install(router)
        repo = Repo.init(vcs_root)
        (vcs_root / ".tether" / "secrets.toml").write_text(
            f'[backends.neon]\napi_url = "{BASE}"\n'
        )
        (vcs_root / ".tether" / "secrets.toml").chmod(0o600)
        repo = Repo.find(vcs_root)
        backend = repo.backend_for("neon")
        monkeypatch.setattr(backend, "_probe", probe)
        monkeypatch.setattr(backend, "_active_writers", lambda uri: 0)
        repo.add("db", "neon", dict(LOCATOR))
        res = repo.commit("baseline")
        assert res.pinned["db"] is not None
        pins_after_baseline = len(backend.list_pins(LOCATOR))
        # One branch listing per fingerprint: `_require_branch`, `_lineage`,
        # and the rest share it within the call (and never across two).
        before = fake.listings
        backend.fingerprint(LOCATOR, None)
        assert fake.listings == before + 1
        backend.fingerprint(LOCATOR, None)
        assert fake.listings == before + 2  # not reused across calls

        repo.new(bookmark="work", eager=True)
        wref = repo.workspace.working_refs["db"]
        # Untouched: clean, and a commit has nothing to pin.
        (db,) = repo.status(do_snapshot=True).objects
        assert not db.changed
        assert repo.plan_commit("nothing").is_empty
        assert len(backend.list_pins(LOCATOR)) == pins_after_baseline

        # Writes on the fork: modified, pinned once, then clean again.
        probe.write(wref)
        (db,) = repo.status(do_snapshot=True).objects
        assert db.changed
        res = repo.commit("work")
        assert res.pinned["db"] is not None
        assert len(backend.list_pins(LOCATOR)) == pins_after_baseline + 1
        (db,) = repo.status(do_snapshot=True).objects
        assert not db.changed

        # `new` on the bookmark reuses the branch: no --discard needed.
        plan = repo.plan_new("work", eager=True)
        assert [a.op for a in plan.actions if a.key == "db"] == ["reuse"]
        repo.new("work", eager=True)
        assert repo.workspace.working_refs["db"] == wref

        # Back on the trunk; the bookmark's branch is judged by gc.
        repo.new("main")
        repo.vcs.bookmark_delete("work")
        report = repo.gc(dry_run=True, prune_bookmarks=True)
        assert wref in report.kept_working_refs.get("db", []) or wref in (
            report.deleted_working_refs.get("db", [])
        )
