"""Neon (serverless Postgres) backend (Forkable).

Model:

- **working ref**: a Neon branch (with a lazily-created ``read_write`` endpoint).
- **state**: ``{lsn, next_xid}``. ``lsn`` (``pg_current_wal_flush_lsn``) is what we
  pin; ``next_xid`` (``pg_snapshot_xmax``) only advances when a writing
  transaction ran, so it is the reliable "changed" signal (LSN drifts on
  checkpoints/autovacuum without user writes).
- **pin**: a *protected* child branch ``tether.<pin_id>`` created at
  ``parent_lsn=<lsn>`` with no compute endpoint -- durable and free to keep.
- **fork**: a child branch off the pin; a ``read_write`` endpoint is created on
  first :meth:`open`.

Neon cannot merge or promote a child into its parent, so production writes
should use ``write = track`` on ``main``; child pins deepen the branch tree and
can only be garbage-collected leaf-first.
"""

from __future__ import annotations

import os
from typing import Any

from tether.backends.base import (
    Capability,
    ObjectBackend,
    VerifyReport,
    VerifyStatus,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import Handle, NeonHandle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin

_DEFAULT_API = "https://console.neon.tech/api/v2"
_WORKING_PREFIX = ref_for_pin("ws.")  # "tether.ws."


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

    def get(self, path: str, **params: Any) -> dict:
        resp = self._client.get(path, params={k: v for k, v in params.items() if v})
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict) -> dict:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        resp = self._client.delete(path)
        if resp.status_code not in (200, 404):
            resp.raise_for_status()


class NeonBackend(ObjectBackend):
    kind = "neon"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.NEEDS_QUIESCENCE
        | Capability.RETENTION_BOUND
        | Capability.BRANCH_IS_STORAGE
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._api_obj: _NeonApi | None = None

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

    def _source_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", "main"))

    def _branches(self, project_id: str) -> list[dict]:
        return self._api.get(f"/projects/{project_id}/branches").get("branches", [])

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

    def _connection_uri(self, project_id: str, branch_id: str, locator: Locator) -> str:
        data = self._api.get(
            f"/projects/{project_id}/connection_uri",
            branch_id=branch_id,
            database_name=locator.get("database"),
            role_name=locator.get("role"),
        )
        return str(data["uri"])

    # Overridable seam: the live SQL probe (monkeypatched in tests).
    def _probe(self, conn_uri: str) -> tuple[str, str]:
        import psycopg

        with psycopg.connect(conn_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_current_wal_flush_lsn()::text, "
                "pg_snapshot_xmax(pg_current_snapshot())::text"
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - defensive
                raise BackendError("empty LSN probe result", kind="neon")
            return str(row[0]), str(row[1])

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
        return {
            "project_id": self._project(locator),
            "branch": self._source_branch(locator),
            "database": locator.get("database"),
            "role": locator.get("role"),
        }

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
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
        uri = self._connection_uri(project_id, branch["id"], locator)
        lsn, next_xid = self._probe(uri)
        # An LSN is only meaningful on its own timeline, so the state names the
        # branch it was read from; pin/fork/open hang off that branch.
        return {"lsn": lsn, "next_xid": next_xid, "branch": str(branch["name"])}

    def check_quiescence(self, locator: Locator, working_ref: str | None) -> None:
        project_id = self._project(locator)
        branch = self._require_branch(
            project_id, working_ref or self._source_branch(locator)
        )
        uri = self._connection_uri(project_id, branch["id"], locator)
        if self._active_writers(uri) > 0:
            raise BackendError(
                "active writers detected; commit with --force to override",
                kind="neon",
            )

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        project_id = self._project(locator)
        ref = ref_for_pin(pin_id)
        existing = self._branch_by_name(project_id, ref)
        if existing is None:
            parent = self._require_branch(project_id, str(state["branch"]))
            self._api.post(
                f"/projects/{project_id}/branches",
                {
                    "branch": {
                        "name": ref,
                        "parent_id": parent["id"],
                        "parent_lsn": str(state["lsn"]),
                        "protected": True,
                    },
                    "endpoints": [],
                },
            )
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        project_id = self._project(locator)
        br = self._branch_by_name(project_id, pin.ref)
        if br is not None:
            self._api.delete(f"/projects/{project_id}/branches/{br['id']}")

    def list_pins(self, locator: Locator) -> set[str]:
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
        parent = self._branch_by_name(project_id, str(state["branch"]))
        if parent is not None and br.get("parent_id") != parent["id"]:
            return VerifyReport(
                VerifyStatus.DRIFTED,
                f"pin hangs off {br.get('parent_id')}, not {state['branch']}",
            )
        if br.get("last_reset_at"):
            return VerifyReport(VerifyStatus.DRIFTED, "branch was reset")
        if self._endpoints_for(project_id, br["id"]):
            return VerifyReport(
                VerifyStatus.DRIFTED, "pin branch unexpectedly has an endpoint"
            )
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        project_id = self._project(locator)
        if self._branch_by_name(project_id, name) is not None:
            return name
        if isinstance(source, Pin):
            parent = self._require_branch(project_id, source.ref)
            branch: dict = {"name": name, "parent_id": parent["id"]}
        else:
            # Recorded state (no pin branch): fork the state's branch at the
            # recorded LSN; only possible while it is inside the history window.
            parent = self._require_branch(project_id, str(source["branch"]))
            branch = {
                "name": name,
                "parent_id": parent["id"],
                "parent_lsn": str(source["lsn"]),
            }
        self._api.post(f"/projects/{project_id}/branches", {"branch": branch})
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        project_id = self._project(locator)
        if ref == self._source_branch(locator):
            return
        br = self._branch_by_name(project_id, ref)
        if br is not None:
            self._api.delete(f"/projects/{project_id}/branches/{br['id']}")

    def list_working_refs(self, locator: Locator) -> list[str]:
        return sorted(
            str(br.get("name", ""))
            for br in self._branches(self._project(locator))
            if str(br.get("name", "")).startswith(WORKING_REF_PREFIX)
        )

    def _ensure_endpoint(self, project_id: str, branch_id: str, ep_type: str) -> None:
        for ep in self._endpoints_for(project_id, branch_id):
            if ep.get("type") == ep_type:
                return
        self._api.post(
            f"/projects/{project_id}/endpoints",
            {"endpoint": {"branch_id": branch_id, "type": ep_type}},
        )

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        project_id = self._project(locator)
        if isinstance(target, Pin):
            br = self._require_branch(project_id, target.ref)
            self._ensure_endpoint(project_id, br["id"], "read_only")
            uri = self._connection_uri(project_id, br["id"], locator)
            return NeonHandle(
                key=target.ref, read_only=True, url=uri, branch=target.ref
            )
        if isinstance(target, dict):
            # Time-travel read on the state's branch within the history window.
            br = self._require_branch(project_id, str(target["branch"]))
            uri = self._connection_uri(project_id, br["id"], locator)
            sep = "&" if "?" in uri else "?"
            uri = f"{uri}{sep}options=neon_lsn:{target['lsn']}"
            return NeonHandle(
                key=br["name"], read_only=True, url=uri, branch=br["name"]
            )
        name = target or self._source_branch(locator)
        br = self._require_branch(project_id, name)
        if not read_only:
            self._ensure_endpoint(project_id, br["id"], "read_write")
        uri = self._connection_uri(project_id, br["id"], locator)
        return NeonHandle(key=name, read_only=read_only, url=uri, branch=str(name))


def _factory(config: dict) -> NeonBackend:
    return NeonBackend(config)


register_backend("neon", _factory)
