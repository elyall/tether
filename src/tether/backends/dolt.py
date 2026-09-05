"""Dolt backend (Forkable).

Dolt is git for SQL, so the mapping is one-to-one: a branch is the working ref,
its head commit hash is the state, a tag is the pin, and a branch created from
the tag is a fork. tether talks to a ``dolt sql-server`` (or Hosted Dolt /
DoltHub) over the MySQL protocol using the ``dolt_*`` system tables and
``DOLT_TAG`` / ``DOLT_BRANCH`` procedures; handles are revision-database URLs
(``db/branch`` for writes, ``db/tag`` or ``db/<hash>`` for read-only reads).

Uncommitted working-set changes on a branch are not part of any commit; a dirty
branch is reported in the state and refused at pin time.

Locator: ``host``, ``port`` (3306), ``database``, ``branch`` (``main``), optional
``user``; or a ``url`` (``mysql://user@host:port/database``). The password comes
from the environment variable named by ``[backends.dolt] password_env``
(default ``DOLT_PASSWORD``); ``user_env`` (default ``DOLT_USER``) supplies the
user when the locator has none. Nothing secret is written to manifests.

The SQL surface is isolated behind :class:`DoltClient` so tests (and other
transports, e.g. the ``dolt`` CLI) can substitute an implementation.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

from tether.backends.base import (
    Capability,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    VerifyReport,
    VerifyStatus,
    base_at,
    iso_utc,
    register_backend,
)
from tether.errors import BackendError
from tether.handles import DoltHandle, Handle
from tether.manifest import WORKING_REF_PREFIX, Locator, Pin, State, ref_for_pin

MAIN = "main"
DEFAULT_PORT = 3306


class DoltClient(Protocol):
    """The handful of Dolt operations tether needs, for one database."""

    def branch_head(self, branch: str) -> tuple[str, bool] | None:
        """Return ``(commit hash, dirty)`` for ``branch`` or ``None`` if absent."""

    def tag_hash(self, name: str) -> str | None:
        """Return the commit hash a tag points at, or ``None``."""

    def list_tags(self) -> list[str]: ...

    def create_tag(self, name: str, ref: str) -> None: ...

    def delete_tag(self, name: str) -> None: ...

    def commit_exists(self, commit: str) -> bool: ...

    def create_branch(self, name: str, ref: str) -> None: ...

    def delete_branch(self, name: str) -> None: ...

    def list_branches(self) -> list[str]: ...

    def diff_summary(self, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
        """Rows of ``dolt_diff_summary``: table_name, diff_type, data/schema_change."""

    def diff_stat(self, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
        """Rows of ``dolt_diff_stat``: table_name, rows_added/deleted/modified, ..."""

    def resolve(self, ref: str) -> str | None:
        """Resolve a branch, tag, or commit hash to a commit hash (``HASHOF``)."""

    def log(self, ref: str, limit: int) -> list[dict[str, Any]]:
        """Rows of ``DOLT_LOG(ref)``: commit_hash, committer, date, message."""


class SqlDoltClient:
    """:class:`DoltClient` over the MySQL protocol (PyMySQL)."""

    def __init__(self, connect: Callable[..., Any], **connect_kwargs: Any) -> None:
        self._connect = connect
        self._kwargs = connect_kwargs

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = self._connect(**self._kwargs)
        try:
            with con.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            return [tuple(r) for r in rows]
        finally:
            con.close()

    def _rows_as_dicts(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        con = self._connect(**self._kwargs)
        try:
            with con.cursor() as cur:
                cur.execute(sql, params)
                if not cur.description:
                    return []
                columns = [str(d[0]) for d in cur.description]
                return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        finally:
            con.close()

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        rows = self._run(sql, params)
        return rows[0] if rows else None

    def branch_head(self, branch: str) -> tuple[str, bool] | None:
        try:
            row = self._one(
                "SELECT hash, dirty FROM dolt_branches WHERE name = %s", (branch,)
            )
        except Exception:  # older Dolt without the `dirty` column
            row = self._one(
                "SELECT hash, FALSE FROM dolt_branches WHERE name = %s", (branch,)
            )
        if row is None:
            return None
        return str(row[0]), bool(row[1])

    def tag_hash(self, name: str) -> str | None:
        row = self._one("SELECT tag_hash FROM dolt_tags WHERE tag_name = %s", (name,))
        return str(row[0]) if row else None

    def list_tags(self) -> list[str]:
        return [str(r[0]) for r in self._run("SELECT tag_name FROM dolt_tags")]

    def create_tag(self, name: str, ref: str) -> None:
        self._run("CALL DOLT_TAG(%s, %s)", (name, ref))

    def delete_tag(self, name: str) -> None:
        self._run("CALL DOLT_TAG('-d', %s)", (name,))

    def commit_exists(self, commit: str) -> bool:
        return (
            self._one("SELECT 1 FROM dolt_commits WHERE commit_hash = %s", (commit,))
            is not None
        )

    def create_branch(self, name: str, ref: str) -> None:
        self._run("CALL DOLT_BRANCH(%s, %s)", (name, ref))

    def delete_branch(self, name: str) -> None:
        self._run("CALL DOLT_BRANCH('-D', %s)", (name,))

    def list_branches(self) -> list[str]:
        return [str(r[0]) for r in self._run("SELECT name FROM dolt_branches")]

    def diff_summary(self, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
        return self._rows_as_dicts(
            "SELECT * FROM dolt_diff_summary(%s, %s)", (from_ref, to_ref)
        )

    def diff_stat(self, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
        return self._rows_as_dicts(
            "SELECT * FROM dolt_diff_stat(%s, %s)", (from_ref, to_ref)
        )

    def resolve(self, ref: str) -> str | None:
        try:
            row = self._one("SELECT HASHOF(%s)", (ref,))
        except Exception:  # unknown ref raises in Dolt
            return None
        return str(row[0]) if row and row[0] else None

    def log(self, ref: str, limit: int) -> list[dict[str, Any]]:
        return self._rows_as_dicts(
            "SELECT commit_hash, committer, date, message FROM DOLT_LOG(%s) LIMIT %s",
            (ref, int(limit)),
        )


class DoltBackend(ObjectBackend):
    kind = "dolt"
    capabilities = (
        Capability.FINGERPRINT
        | Capability.ADDRESSABLE
        | Capability.PIN
        | Capability.FORK
        | Capability.ATOMIC_REF
        | Capability.DIFF
        | Capability.HISTORY
    )

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}

    # -- addressing ------------------------------------------------------ #
    def _endpoint(self, locator: Locator) -> tuple[str, int, str, str | None]:
        """Return ``(host, port, database, user)`` from ``url`` or fields."""
        url = locator.get("url")
        if url:
            parsed = urlparse(str(url))
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or DEFAULT_PORT
            database = parsed.path.strip("/").split("/", 1)[0]
            user = parsed.username
        else:
            host = str(locator.get("host") or "127.0.0.1")
            port = int(locator.get("port") or DEFAULT_PORT)
            database = str(locator.get("database") or "")
            user = locator.get("user")
        if not database:
            raise BackendError("dolt locator needs 'database' (or a url)", kind="dolt")
        user = user or os.environ.get(str(self._config.get("user_env", "DOLT_USER")))
        return host, port, database, str(user) if user else None

    def _base_branch(self, locator: Locator) -> str:
        return str(locator.get("branch", MAIN))

    def _client(self, locator: Locator) -> DoltClient:
        """Return a client for the locator's database. Tests replace this seam."""
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendError(
                "the dolt extra is required (`pip install tether-vcs[dolt]`)",
                kind="dolt",
            ) from exc
        host, port, database, user = self._endpoint(locator)
        password = os.environ.get(
            str(self._config.get("password_env", "DOLT_PASSWORD"))
        )
        return SqlDoltClient(
            pymysql.connect,
            host=host,
            port=port,
            user=user or "root",
            password=password or "",
            database=database,
            autocommit=True,
        )

    def _url(self, locator: Locator, ref: str) -> str:
        host, port, database, user = self._endpoint(locator)
        auth = f"{user}@" if user else ""
        return f"mysql://{auth}{host}:{port}/{database}/{ref}"

    # -- protocol -------------------------------------------------------- #
    def identity(self, locator: Locator) -> Locator:
        host, port, database, _ = self._endpoint(locator)
        return {"host": host, "port": port, "database": database}

    def fingerprint(self, locator: Locator, working_ref: str | None) -> State:
        client = self._client(locator)
        if working_ref is None and (at := base_at(locator)) is not None:
            commit = client.resolve(at)
            if commit is None:
                raise BackendError(f"cannot resolve dolt ref {at!r}", kind="dolt")
            return {"commit": commit}
        branch = working_ref or self._base_branch(locator)
        head = client.branch_head(branch)
        if head is None:
            raise BackendError(f"dolt branch not found: {branch}", kind="dolt")
        commit, dirty = head
        state: State = {"commit": commit}
        if dirty:
            state["dirty"] = True
        return state

    def history(
        self,
        locator: Locator,
        ref: str | None = None,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        client = self._client(locator)
        start = ref or base_at(locator) or self._base_branch(locator)
        pointing: dict[str, list[str]] = {}
        for tag in client.list_tags():
            commit = client.tag_hash(tag)
            if commit:
                pointing.setdefault(commit, []).append(tag)
        head = client.branch_head(start)
        if head is not None:
            pointing.setdefault(head[0], []).insert(0, start)
        return [
            HistoryEntry(
                id=str(row.get("commit_hash")),
                when=iso_utc(row.get("date")),
                message=str(row.get("message") or ""),
                refs=pointing.get(str(row.get("commit_hash")), []),
            )
            for row in client.log(start, limit)
        ]

    def pin(self, locator: Locator, state: State, pin_id: str) -> Pin:
        if state.get("dirty"):
            raise BackendError(
                "dolt branch has uncommitted changes; DOLT_COMMIT or DOLT_RESET first",
                kind="dolt",
            )
        client = self._client(locator)
        ref = ref_for_pin(pin_id)
        commit = str(state["commit"])
        existing = client.tag_hash(ref)
        if existing is None:
            client.create_tag(ref, commit)
            existing = client.tag_hash(ref)
        if existing != commit:
            raise BackendError(
                f"tag {ref} already points at {existing}, not {commit}", kind="dolt"
            )
        return Pin(id=pin_id, ref=ref)

    def unpin(self, locator: Locator, pin: Pin) -> None:
        client = self._client(locator)
        if client.tag_hash(pin.ref) is not None:
            client.delete_tag(pin.ref)

    def list_pins(self, locator: Locator) -> set[str]:
        prefix = ref_for_pin("")
        return {
            t[len(prefix) :]
            for t in self._client(locator).list_tags()
            if t.startswith(prefix)
        }

    def verify(
        self,
        locator: Locator,
        state: State,
        pin: Pin | None,
        deep: bool,
    ) -> VerifyReport:
        client = self._client(locator)
        commit = str(state["commit"])
        if pin is not None:
            actual = client.tag_hash(pin.ref)
            if actual is None:
                return VerifyReport(VerifyStatus.MISSING, f"tag {pin.ref} missing")
            if actual != commit:
                return VerifyReport(
                    VerifyStatus.DRIFTED, f"{pin.ref} -> {actual}, expected {commit}"
                )
            return VerifyReport(VerifyStatus.OK)
        if not deep:
            return VerifyReport(
                VerifyStatus.UNKNOWN, "pass --deep to look up the commit"
            )
        if not client.commit_exists(commit):
            return VerifyReport(VerifyStatus.MISSING, f"commit {commit} not found")
        return VerifyReport(VerifyStatus.OK)

    def fork(self, locator: Locator, source: Pin | State, name: str) -> str:
        client = self._client(locator)
        if isinstance(source, Pin):
            target = client.tag_hash(source.ref)
            if target is None:
                raise BackendError(
                    f"tag {source.ref} missing; cannot fork", kind="dolt"
                )
            origin = source.ref
        else:
            # Recorded state (no tag): fork straight from the commit hash.
            origin = target = str(source["commit"])
            if not client.commit_exists(target):
                raise BackendError(f"commit {target} missing; cannot fork", kind="dolt")
        head = client.branch_head(name)
        if head is not None and head[0] == target and not head[1]:
            return name
        if head is not None:
            client.delete_branch(name)  # reset semantics, like icechunk
        client.create_branch(name, origin)
        return name

    def delete_working_ref(self, locator: Locator, ref: str) -> None:
        if ref == self._base_branch(locator) or ref == MAIN:
            return
        client = self._client(locator)
        if client.branch_head(ref) is not None:
            client.delete_branch(ref)

    def list_working_refs(self, locator: Locator) -> list[str]:
        return sorted(
            b
            for b in self._client(locator).list_branches()
            if b.startswith(WORKING_REF_PREFIX)
        )

    def open(
        self,
        locator: Locator,
        target: str | Pin | State | None,
        read_only: bool,
    ) -> Handle:
        _, _, database, _ = self._endpoint(locator)
        if isinstance(target, Pin):
            commit = self._client(locator).tag_hash(target.ref)
            if commit is None:
                raise BackendError(f"tag {target.ref} missing", kind="dolt")
            ref: str = target.ref
            ro = True
        elif isinstance(target, dict):
            commit = str(target["commit"])
            ref = commit
            ro = True
        elif target is None and read_only and (at := base_at(locator)) is not None:
            ref = at
            commit = self._client(locator).resolve(at)
            ro = True
        else:
            ref = target or self._base_branch(locator)
            commit = None
            ro = read_only
        return DoltHandle(
            key=f"{database}",
            read_only=ro,
            url=self._url(locator, ref),
            database=database,
            ref=ref,
            commit=commit,
        )

    def diff(
        self,
        locator: Locator,
        a: State,
        b: State,
        *,
        listings: Listings = (None, None),
    ) -> ObjectDiff:
        """Per-table diff from ``dolt_diff_summary`` + ``dolt_diff_stat``.

        Dolt diffs are computed on prolly trees, so cost scales with the change
        rather than table size.
        """
        ca, cb = str(a["commit"]), str(b["commit"])
        out = ObjectDiff(unit="tables")
        if ca == cb:
            if a.get("dirty") != b.get("dirty"):
                out.note = f"dirty {a.get('dirty')} -> {b.get('dirty')}"
            return out
        client = self._client(locator)
        stats = {str(r.get("table_name")): r for r in client.diff_stat(ca, cb)}
        for row in client.diff_summary(ca, cb):
            table = str(row.get("table_name") or row.get("to_table_name") or "?")
            diff_type = str(row.get("diff_type", "modified"))
            change = {"added": "added", "dropped": "removed", "renamed": "renamed"}.get(
                diff_type, "modified"
            )
            if change == "renamed":
                old, new = (
                    row.get("from_table_name", "?"),
                    row.get("to_table_name", "?"),
                )
                table = f"{old} -> {new}"
            stat = stats.get(table, {})
            parts = [
                f"{sign}{stat[k]} rows"
                for k, sign in (
                    ("rows_added", "+"),
                    ("rows_deleted", "-"),
                    ("rows_modified", "~"),
                )
                if stat.get(k) not in (None, 0, "0")
            ]
            if row.get("schema_change") in (1, True, "1", "true"):
                parts.append("schema changed")
            out.add(table, change, ", ".join(parts))
        return out


def _factory(config: dict) -> DoltBackend:
    return DoltBackend(config)


register_backend("dolt", _factory)
