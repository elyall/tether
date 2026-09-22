"""Neon (serverless Postgres) backend (Forkable).

Model:

- **working ref**: a Neon branch (with a lazily-created ``read_write`` endpoint).
- **state**: ``{lsn, commit_xid}``. ``lsn`` (``pg_current_wal_flush_lsn``) is what
  we pin; ``commit_xid``, the newest committed transaction id, only moves when
  a writing transaction committed, so it is the "changed" signal (LSN drifts
  on checkpoints/autovacuum without user writes, and ``nextXid`` jumps when a
  compute restarts).
- **pin**: a child branch ``tether.<pin_id>`` (protected on request) created at
  ``parent_lsn=<lsn>`` with no compute endpoint -- durable and free to keep.
- **fork**: a child branch off the pin; a ``read_write`` endpoint is created on
  first use.

Neon cannot merge or promote a child into its parent, so production writes
should happen on the trunk bookmark (straight to ``main``); child pins deepen
the branch tree and can only be garbage-collected leaf-first.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

from tether.backends.base import (
    Capability,
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    check_expected,
    register_backend,
    wrap_library_errors,
)
from tether.errors import BackendError
from tether.handles import Handle, NeonHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin

_DEFAULT_API = "https://console.neon.tech/api/v2"
_WORKING_PREFIX = ref_for_pin("ws.")  # "tether.ws."

# Locked (another operation on the project is running), throttled, briefly
# unavailable: Neon applied nothing, so the request is safe to send again.
_RETRY_STATUSES = frozenset({423, 429, 503})
_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

# How far below `nextXid` the probe looks for the newest committed xid. A
# compute restart rounds `nextXid` up (to a multiple of 1024, 128 on newer
# pageservers), so this spans many restarts with no write in between.
_XID_WINDOW = 65536

_PROBE_SQL = f"""
SELECT pg_current_wal_flush_lsn()::text,
       (SELECT max(x) FROM generate_series(greatest(n - {_XID_WINDOW}, 3), n - 1) x
         WHERE pg_xact_status(x::text::xid8) = 'committed')::text,
       n::text
FROM (SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint AS n) s
"""


class _NeonApi:
    """Thin Neon control-plane client over httpx."""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_API) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        self.generation = 0
        """Bumped by every mutating call; a cached listing is only reused
        while the generation it was taken at is current."""

    @staticmethod
    def _ok(resp: Any, *, allow: tuple[int, ...] = ()) -> Any:
        """Raise a `BackendError` carrying Neon's message on an HTTP error."""
        if resp.is_success or resp.status_code in allow:
            return resp
        try:
            detail = resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        raise BackendError(
            f"Neon API {resp.request.method} {resp.request.url.path}: "
            f"{resp.status_code} {detail}".strip(),
            kind="neon",
        )

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request, retried with backoff while Neon answers 423/429/503."""
        for delay in (*_RETRY_DELAYS, None):
            resp = self._client.request(method, path, **kwargs)
            if resp.status_code not in _RETRY_STATUSES or delay is None:
                return resp
            after = resp.headers.get("Retry-After", "")
            time.sleep(min(float(after), 60.0) if after.isdigit() else delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, path: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v}
        return self._ok(self._send("GET", path, params=params)).json()

    def post(self, path: str, body: dict) -> dict:
        self.generation += 1
        return self._ok(self._send("POST", path, json=body)).json()

    def delete(self, path: str) -> None:
        self.generation += 1
        self._ok(self._send("DELETE", path), allow=(404,))

    def patch(self, path: str, body: dict) -> dict:
        self.generation += 1
        return self._ok(self._send("PATCH", path, json=body)).json()


@wrap_library_errors
class NeonBackend(ObjectBackend):
    kind = "neon"
    MATURITY = "experimental"
    SAFE_CONFIG_KEYS = frozenset()  # api_url / api_key_env: secrets.toml only
    # The LSN advances on checkpoints and autovacuum with no user write; it is
    # where a pin branch is cut, not what identifies the data. `commit_xid` is.
    # `timeline` is the branch the state was read from -- needed to pin, fork,
    # and open (an LSN is only meaningful on its own timeline) but not part of
    # the content: an untouched fork reports its parent's `branch`, as a Lance
    # fork reports its parent's version, so forks compare equal to the pin.
    VOLATILE_KEYS = frozenset({"lsn", "timeline"})
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.NEEDS_QUIESCENCE
        | Capability.RETENTION_BOUND
        | Capability.BRANCH_IS_STORAGE
    )

    @staticmethod
    def _library_errors() -> tuple[type[BaseException], ...]:
        import httpx

        errors: tuple[type[BaseException], ...] = (httpx.HTTPError, OSError)
        try:
            import psycopg
        except ImportError:  # pragma: no cover - optional dep
            return errors
        return (*errors, psycopg.Error)

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._api_obj: _NeonApi | None = None
        # project id -> (api generation, branches). One protocol call needs
        # the listing several times (`_require_branch`, `_lineage`, a pin's
        # parent); a project with hundreds of pin branches must not pay for
        # each. Cleared at the start of every protocol call (`_fresh`) and
        # ignored after any mutation, so it never outlives one operation.
        self._branch_cache: dict[str, tuple[int, list[dict]]] = {}

    def _fresh(self) -> None:
        """Forget cached listings: called on entry to every protocol method,
        so a listing is taken at most once per operation and never reused
        across two."""
        self._branch_cache.clear()

    # -- api / helpers --------------------------------------------------- #
    @property
    def _api(self) -> _NeonApi:
        if self._api_obj is None:
            env = self._config.get("api_key_env", "NEON_API_KEY")
            key = os.environ.get(env)
            if not key:
                raise BackendError(f"Neon API key not found in ${env}", kind="neon")
            base = self._config.get("api_url", _DEFAULT_API)
            self._api_obj = _NeonApi(key, base)
        return self._api_obj

    def _project(self, locator: Locator) -> str:
        pid = locator.get("project_id")
        if not pid:
            raise BackendError("neon locator needs 'project_id'", kind="neon")
        return str(pid)

    def validate_locator(self, locator: Locator) -> None:
        # The API builds no connection URI without a database and a role.
        missing = [k for k in ("project_id", "database", "role") if not locator.get(k)]
        if missing:
            raise BackendError(
                f"neon locator needs {', '.join(repr(k) for k in missing)} "
                "(`--project-id`, `--database`, `--role`)",
                kind="neon",
            )

    def _source_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    def _branches(self, project_id: str) -> list[dict]:
        cached = self._branch_cache.get(project_id)
        if cached is not None and cached[0] == self._api.generation:
            return cached[1]
        out = self._list_branches(project_id)
        self._branch_cache[project_id] = (self._api.generation, out)
        return out

    def _list_branches(self, project_id: str) -> list[dict]:
        # The branch list is paginated: follow the cursor until a page comes
        # back without one (or empty). A project with more branches than one
        # page holds -- every pin is a branch -- must not lose the tail: gc
        # would take the unseen pins for absent and repin or miss them.
        out: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self._api.get(
                f"/projects/{project_id}/branches", limit=500, cursor=cursor
            )
            branches = page.get("branches", [])
            out.extend(branches)
            pagination = page.get("pagination") or {}
            cursor = pagination.get("cursor") or pagination.get("next")
            if not branches or not cursor or cursor in seen:
                return out
            seen.add(cursor)

    def _branch_by_name(self, project_id: str, name: str) -> dict | None:
        for br in self._branches(project_id):
            if br.get("name") == name or br.get("id") == name:
                return br
        return None

    def _require_branch(self, project_id: str, name: str) -> dict:
        br = self._branch_by_name(project_id, name)
        if br is None:
            raise BackendError(f"branch {name!r} not found", kind="neon")
        return br

    def _endpoints_for(self, project_id: str, branch_id: str) -> list[dict]:
        eps = self._api.get(f"/projects/{project_id}/endpoints").get("endpoints", [])
        return [e for e in eps if e.get("branch_id") == branch_id]

    def _connection_uri(
        self, project_id: str, branch_id: str, endpoint_id: str, locator: Locator
    ) -> str:
        # Without `endpoint_id` Neon answers with the branch's read-write
        # compute, which pins never have and forks lack until first use.
        data = self._api.get(
            f"/projects/{project_id}/connection_uri",
            branch_id=branch_id,
            endpoint_id=endpoint_id,
            database_name=locator.get("database"),
            role_name=locator.get("role"),
        )
        return str(data["uri"])

    def _branch_uri(self, project_id: str, branch_id: str, locator: Locator) -> str:
        endpoint = self._ensure_endpoint(project_id, branch_id, "read_write")
        return self._connection_uri(project_id, branch_id, endpoint, locator)

    # Overridable seam: the live SQL probe (monkeypatched in tests).
    def _probe(self, conn_uri: str) -> tuple[str, str]:
        """`(lsn, commit_xid)`: the WAL position and the newest committed xid.

        With no commit in the window below `nextXid`, `commit_xid` is
        `<nextXid`, which a later commit can never equal.
        """
        import psycopg

        with psycopg.connect(conn_uri) as conn, conn.cursor() as cur:
            cur.execute(_PROBE_SQL)
            row = cur.fetchone()
            if row is None:  # pragma: no cover - defensive
                raise BackendError("empty LSN probe result", kind="neon")
            lsn, committed, next_xid = row
            return str(lsn), str(committed) if committed else f"<{next_xid}"

    def _active_writers(self, conn_uri: str) -> int:
        import psycopg

        with psycopg.connect(conn_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'active' AND pid <> pg_backend_pid() "
                "AND backend_type = 'client backend'"
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        # What Neon pins and forks is a project timeline point: a branch at
        # an LSN is a snapshot of every database and role in the project.
        # `database` and `role` are how you connect to that snapshot, not
        # what it is, so neither changes pin ids. Two objects on two
        # databases of one project at one commit therefore share one pin
        # branch (and, through `branch_scope`, one working branch) instead
        # of cutting two identical ones off the same LSN.
        return {
            "project_id": self._project(locator),
            "branch": self._source_branch(locator),
        }

    def branch_scope(self, locator: Locator) -> str:
        # Branches are project-wide: two databases in one project share them.
        return f"neon:{self._project(locator)}"

    def ref_namespace(self, locator: Locator) -> str:
        # Pins are branches too, so they are listed project-wide.
        return f"neon:{self._project(locator)}"

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        """`{lsn, commit_xid, branch[, timeline]}` for the branch read.

        Content is `{commit_xid, branch}` where `branch` is the *lineage* (the
        object's source branch when the branch read descends from it) and
        `lsn`/`timeline` are volatile address keys. Two sibling branches of
        the same lineage that have consumed the same number of transactions
        therefore compare equal -- Neon exposes no content hash and no
        history query to tell them apart. That is why Neon is
        `BRANCH_IS_STORAGE`: gc never judges a branch by its state alone.
        """
        self._fresh()
        if locator.get("at"):
            raise BackendError(
                "neon does not support a detached base (`at`); an LSN is only "
                "meaningful within the history window -- pin from a branch instead",
                kind="neon",
            )
        project_id = self._project(locator)
        branch = self._require_branch(
            project_id, working_ref or self._source_branch(locator)
        )
        uri = self._branch_uri(project_id, branch["id"], locator)
        lsn, commit_xid = self._probe(uri)
        # `branch` is the lineage -- the object's source branch when this one
        # descends from it -- so that a fork with no writes has the same
        # content state as the pin it was cut from; `timeline` is the branch
        # actually read, which pin/fork/open hang off.
        source = self._source_branch(locator)
        lineage = self._lineage(project_id, branch, source)
        state: State = {"lsn": lsn, "commit_xid": commit_xid, "branch": lineage}
        if str(branch["name"]) != lineage:
            state["timeline"] = str(branch["name"])
        return state

    def _lineage(self, project_id: str, branch: dict, source: str) -> str:
        """`source` if `branch` descends from it (or is it), else the branch's
        own name: a branch cut from elsewhere is its own lineage."""
        by_id = {b["id"]: b for b in self._branches(project_id)}
        seen: set[str] = set()
        cur: dict | None = branch
        while cur is not None and cur["id"] not in seen:
            if str(cur.get("name")) == source:
                return source
            seen.add(cur["id"])
            parent = cur.get("parent_id")
            cur = by_id.get(parent) if parent else None
        return str(branch["name"])

    @staticmethod
    def _timeline(state: Mapping[str, Any]) -> str:
        """The branch a state was read from (its LSN is meaningful there)."""
        return str(state.get("timeline") or state["branch"])

    def check_quiescence(self, locator: Locator, working_ref: str | None) -> None:
        self._fresh()
        project_id = self._project(locator)
        branch = self._require_branch(
            project_id, working_ref or self._source_branch(locator)
        )
        uri = self._branch_uri(project_id, branch["id"], locator)
        if self._active_writers(uri) > 0:
            raise BackendError(
                "active writers detected; commit with --force to override",
                kind="neon",
            )

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        self._fresh()
        project_id = self._project(locator)
        ref = ref_for_pin(pin_id)
        existing = self._branch_by_name(project_id, ref)
        if existing is not None:
            parent = self._require_branch(project_id, self._timeline(state))
            if existing.get("parent_id") != parent["id"] or str(
                existing.get("parent_lsn")
            ) != str(state["lsn"]):
                raise BackendError(
                    f"pin {ref} exists but hangs off "
                    f"{existing.get('parent_id')}@{existing.get('parent_lsn')}, not "
                    f"{state['branch']}@{state['lsn']}",
                    kind="neon",
                )
        if existing is None:
            parent = self._require_branch(project_id, self._timeline(state))
            self._api.post(
                f"/projects/{project_id}/branches",
                {
                    "branch": {
                        "name": ref,
                        "parent_id": parent["id"],
                        "parent_lsn": str(state["lsn"]),
                        "protected": self._protect_pins(),
                    },
                    "endpoints": [],
                },
            )
        return Pin(id=pin_id, ref=ref, created=existing is None)

    def _protect_pins(self) -> bool:
        """`[backends.neon] protected_pins = true` in secrets.toml creates
        pins *protected* (Neon refuses to delete a protected branch, so a
        stray console click cannot lose one). Off by default: every pin is a
        branch, Free has no protected branches, and paid plans allow a few."""
        return bool(self._config.get("protected_pins", False))

    def unpin(self, locator: Locator, pin: Pin) -> None:
        self._fresh()
        project_id = self._project(locator)
        br = self._branch_by_name(project_id, pin.ref)
        if br is None:
            return
        if br.get("protected"):
            # Pins are created protected (Neon refuses to delete a protected
            # branch); releasing one lifts the protection first.
            self._api.patch(
                f"/projects/{project_id}/branches/{br['id']}",
                {"branch": {"protected": False}},
            )
        self._api.delete(f"/projects/{project_id}/branches/{br['id']}")
        if self._branch_by_name(project_id, pin.ref) is not None:
            raise BackendError(f"pin {pin.ref} was not deleted", kind="neon")

    def list_pins(self, locator: Locator) -> set[str]:
        self._fresh()
        project_id = self._project(locator)
        prefix = ref_for_pin("")
        out: set[str] = set()
        for br in self._branches(project_id):
            name = br.get("name", "")
            if name.startswith(prefix) and not name.startswith(_WORKING_PREFIX):
                out.add(name[len(prefix) :])
        return out

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        self._fresh()
        project_id = self._project(locator)
        ref = pin.ref if pin is not None else None
        if ref is None:
            return VerifyReport(VerifyStatus.UNKNOWN, "no pin recorded")
        br = self._branch_by_name(project_id, ref)
        if br is None:
            return VerifyReport(VerifyStatus.MISSING, f"branch {ref} missing")
        if str(br.get("parent_lsn")) != str(state["lsn"]):
            return VerifyReport(
                VerifyStatus.DRIFTED,
                f"parent_lsn {br.get('parent_lsn')} != {state['lsn']}",
            )
        parent = self._branch_by_name(project_id, self._timeline(state))
        if parent is not None and br.get("parent_id") != parent["id"]:
            return VerifyReport(
                VerifyStatus.DRIFTED,
                f"pin hangs off {br.get('parent_id')}, not {state['branch']}",
            )
        if br.get("last_reset_at"):
            return VerifyReport(VerifyStatus.DRIFTED, "branch was reset")
        writable = [
            e
            for e in self._endpoints_for(project_id, br["id"])
            if e.get("type") == "read_write"
        ]
        if writable:
            # tether itself attaches a read-only endpoint to serve `open`; a
            # read-write one means someone can change what the pin names.
            return VerifyReport(
                VerifyStatus.DRIFTED, "pin branch has a read-write endpoint"
            )
        return VerifyReport(VerifyStatus.OK)

    def fork(
        self,
        locator: Locator,
        source: Pin | State,
        name: str,
        *,
        expected: State | None = None,
    ) -> str:
        check_expected(self, locator, expected, ref=name)
        self._fresh()
        project_id = self._project(locator)
        if isinstance(source, Pin):
            parent = self._require_branch(project_id, source.ref)
            lsn: str | None = None
        else:
            # Recorded state (no pin branch): fork the state's branch at the
            # recorded LSN; only possible while it is inside the history window.
            parent = self._require_branch(project_id, self._timeline(source))
            lsn = str(source["lsn"])
        branches = self._branches(project_id)
        existing = next((b for b in branches if b.get("name") == name), None)
        if existing is not None:
            # Reset semantics. A branch's parent_id/parent_lsn say where it was
            # *created*, not where its head is, so they cannot tell "nothing
            # written since" from "written and never committed"; always
            # restore. Neon's branch restore is a metadata call.
            children = [b for b in branches if b.get("parent_id") == existing["id"]]
            if not children:
                body: dict = {"source_branch_id": parent["id"]}
                if lsn is not None:
                    body["source_lsn"] = lsn
                self._api.post(
                    f"/projects/{project_id}/branches/{existing['id']}/restore", body
                )
                return name
            # Pins taken on this branch are its children. Neon will only restore
            # a branch with children if the old state is preserved under a new
            # name -- which re-parents every pin onto that backup and breaks
            # their parent checks. Leave the branch (and its pins) alone and
            # start a sibling; the engine records the name fork returns.
            taken = {str(b.get("name", "")) for b in branches}
            n = 2
            while f"{name}.{n}" in taken:
                n += 1
            name = f"{name}.{n}"
        branch: dict = {"name": name, "parent_id": parent["id"]}
        if lsn is not None:
            branch["parent_lsn"] = lsn
        self._api.post(f"/projects/{project_id}/branches", {"branch": branch})
        return name

    def _rename_branch(self, project_id: str, old: str, new: str) -> None:
        br = self._require_branch(project_id, old)
        self._api.patch(
            f"/projects/{project_id}/branches/{br['id']}", {"branch": {"name": new}}
        )

    def rename_pin(self, locator: Locator, old: Pin, state: State, new_id: str) -> Pin:
        # A pin is a branch; working branches forked from it are its children,
        # so pin-then-unpin would fail. Rename in place.
        project_id = self._project(locator)
        new_ref = ref_for_pin(new_id)
        if self._branch_by_name(project_id, new_ref) is None:
            self._rename_branch(project_id, old.ref, new_ref)
        elif self._branch_by_name(project_id, old.ref) is not None:
            self.unpin(locator, old)  # duplicate of an existing pin: drop it
        return Pin(id=new_id, ref=new_ref)

    def rename_working_ref(self, locator: Locator, old: str, new: str) -> str:
        self._rename_branch(self._project(locator), old, new)
        return new

    def working_ref_blockers(self, locator: Locator, ref: str) -> str | None:
        project_id = self._project(locator)
        branches = self._branches(project_id)
        me = next((b for b in branches if b.get("name") == ref), None)
        if me is None:
            return None
        children = sorted(
            str(b.get("name", "")) for b in branches if b.get("parent_id") == me["id"]
        )
        if not children:
            return None
        return (
            f"{len(children)} branch(es) hang off it ({', '.join(children[:3])}"
            f"{', ...' if len(children) > 3 else ''}); Neon deletes a branch only "
            "once its children are gone -- release those pins first"
        )

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        self._fresh()
        project_id = self._project(locator)
        if ref == self._source_branch(locator):
            return
        br = self._branch_by_name(project_id, ref)
        if br is None:
            return
        self._api.delete(f"/projects/{project_id}/branches/{br['id']}")
        # Deletion is asynchronous on Neon's side; re-list rather than trust
        # the status code, as unpin does.
        if self._branch_by_name(project_id, ref) is not None:
            raise BackendError(f"branch {ref} was not deleted", kind="neon")

    def list_working_refs(self, locator: Locator) -> list[str]:
        self._fresh()
        return sorted(
            str(br.get("name", ""))
            for br in self._branches(self._project(locator))
            if str(br.get("name", "")).startswith(WORKING_REF_PREFIX)
        )

    PROMOTE_HINT = (
        "Neon cannot promote a child branch into its parent; work on the trunk "
        "bookmark for databases that must receive writes on main, or copy the "
        "data with pg_dump/psql"
    )

    def _ensure_endpoint(self, project_id: str, branch_id: str, ep_type: str) -> str:
        """The id of the branch's `ep_type` endpoint, created if it has none."""
        for ep in self._endpoints_for(project_id, branch_id):
            if ep.get("type") == ep_type:
                return str(ep["id"])
        created = self._api.post(
            f"/projects/{project_id}/endpoints",
            {"endpoint": {"branch_id": branch_id, "type": ep_type}},
        )
        return str(created["endpoint"]["id"])

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        self._fresh()
        project_id = self._project(locator)
        if isinstance(target, Pin):
            br = self._require_branch(project_id, target.ref)
            ep = self._ensure_endpoint(project_id, br["id"], "read_only")
            uri = self._connection_uri(project_id, br["id"], ep, locator)
            return NeonHandle(
                key=target.ref, read_only=True, url=uri, branch=target.ref
            )
        if isinstance(target, dict):
            # Time-travel read on the state's branch within the history window.
            br = self._require_branch(project_id, self._timeline(target))
            ep = self._ensure_endpoint(project_id, br["id"], "read_only")
            uri = self._connection_uri(project_id, br["id"], ep, locator)
            sep = "&" if "?" in uri else "?"
            uri = f"{uri}{sep}options=neon_lsn:{target['lsn']}"
            return NeonHandle(
                key=br["name"], read_only=True, url=uri, branch=br["name"]
            )
        name = target or self._source_branch(locator)
        br = self._require_branch(project_id, name)
        uri = self._branch_uri(project_id, br["id"], locator)
        return NeonHandle(key=name, read_only=read_only, url=uri, branch=str(name))


def _factory(config: dict) -> NeonBackend:
    return NeonBackend(config)


register_backend("neon", _factory)
