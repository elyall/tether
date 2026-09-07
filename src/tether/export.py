"""A relational view of tether history for registries and SQL tools.

The per-object TOML manifests in the VCS stay the source of truth. This module
derives tables from them -- commits, objects per commit, distinct object
states, refs, optional file listings -- and writes the same rows to SQLite,
Parquet, CSV, JSONL, pyarrow, or a Postgres schema (`publish`). One table
definition (`TABLES`) drives every target, so the shapes cannot drift.

Prior art: git-history's `commits` / `item` / `item_version` split, Quilt's
per-bucket package tables, Dolt's `dolt_log` / `dolt_history_*` system tables.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tether.errors import ConfigError, TetherError
from tether.manifest import (
    ObjectManifest,
    _blake,
    canonical_bytes,
    listing_name,
    manifest_hash,
    read_listing,
)

if TYPE_CHECKING:
    from tether.repo import Repo

__all__ = [
    "SCHEMA_VERSION",
    "TABLES",
    "Column",
    "ExportBundle",
    "PublishReport",
    "Table",
    "TableDef",
    "arrow_schema",
    "build_bundle",
    "postgres_ddl",
    "postgres_upsert_sql",
    "sqlite_ddl",
]

SCHEMA_VERSION = 2
"""Bumped when a table gains, loses, or retypes a column."""

LogicalType = str  # "text" | "int" | "bool" | "json" | "timestamp"


@dataclass(frozen=True)
class Column:
    """One column of an export table."""

    name: str
    type: LogicalType
    """`text`, `int`, `bool`, `json` (a JSON document), or `timestamp` (ISO-8601)."""
    doc: str = ""


@dataclass(frozen=True)
class TableDef:
    """Shape of one export table; shared by every output format."""

    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    doc: str = ""

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)


def _cols(*specs: tuple[str, LogicalType, str]) -> tuple[Column, ...]:
    return tuple(Column(n, t, d) for n, t, d in specs)


TABLES: dict[str, TableDef] = {
    t.name: t
    for t in (
        TableDef(
            "tether_meta",
            _cols(("key", "text", ""), ("value", "text", "")),
            ("key",),
            "schema_version, exported_at, head, vcs, tether_version",
        ),
        TableDef(
            "commits",
            _cols(
                ("commit_id", "text", "Full VCS commit hash"),
                ("change_id", "text", "jj change id; null in git repos"),
                ("author_name", "text", ""),
                ("author_email", "text", ""),
                ("authored_at", "timestamp", "ISO-8601 author time"),
                ("committed_at", "timestamp", "ISO-8601 committer time"),
                ("message", "text", "Full commit message"),
                ("manifest_hash", "text", "Hash of the object set at this commit"),
            ),
            ("commit_id",),
            "One row per VCS commit that was exported",
        ),
        TableDef(
            "commit_parents",
            _cols(
                ("commit_id", "text", ""),
                ("parent_id", "text", ""),
                ("position", "int", "0 = first parent"),
            ),
            ("commit_id", "parent_id"),
            "Parent edges of the commit graph",
        ),
        TableDef(
            "refs",
            _cols(
                ("name", "text", "Bookmark / branch / tag name, or @ / HEAD"),
                ("kind", "text", "bookmark, branch, tag, or head"),
                ("commit_id", "text", ""),
            ),
            ("name", "kind"),
            "Named pointers into history at export time",
        ),
        TableDef(
            "objects",
            _cols(
                ("commit_id", "text", ""),
                ("key", "text", "Object key"),
                ("kind", "text", "Backend kind"),
                (
                    "uri",
                    "text",
                    "Locator `uri` when present; the join key to a catalog",
                ),
                ("locator_json", "json", "Full locator"),
                ("identity_json", "json", "Backend identity (what names the system)"),
                ("identity_hash", "text", "blake2b of the canonical identity"),
                ("policy_write", "text", "fork or track"),
                ("policy_file", "text", "immutable or versioned"),
                ("policy_pin", "text", "native or record"),
                ("state_json", "json", "Recorded state; null before the first commit"),
                ("state_hash", "text", "blake2b of the canonical state"),
                ("pin_id", "text", ""),
                ("pin_ref", "text", "Native ref name (tether.<pin_id>)"),
                ("recoverable", "bool", "False for Observed-tier records"),
                ("captured_at", "timestamp", "When the state was recorded"),
            ),
            ("commit_id", "key"),
            "Every object manifest at every exported commit",
        ),
        TableDef(
            "object_states",
            _cols(
                ("kind", "text", ""),
                ("identity_hash", "text", ""),
                ("state_hash", "text", ""),
                ("identity_json", "json", ""),
                ("state_json", "json", ""),
                ("pin_id", "text", "Same for every commit that records this state"),
                ("first_commit_id", "text", "Earliest exported commit recording it"),
                ("first_committed_at", "timestamp", ""),
            ),
            ("kind", "identity_hash", "state_hash"),
            "Distinct (system, state) pairs: the versions a registry keys on",
        ),
        TableDef(
            "listings",
            _cols(
                ("listing_name", "text", "File name under .tether/listings/"),
                ("kind", "text", ""),
                ("identity_hash", "text", ""),
                ("state_hash", "text", ""),
                ("entries", "int", "Row count in listing_entries"),
            ),
            ("listing_name",),
            "Content-addressed per-file listings (opt-in)",
        ),
        TableDef(
            "listing_entries",
            _cols(
                ("listing_name", "text", ""),
                ("path", "text", "Path relative to the object"),
                ("token", "text", "etag / mtime token"),
                ("size", "int", "Bytes"),
            ),
            ("listing_name", "path"),
            "One row per file in a listing (opt-in)",
        ),
        TableDef(
            "workspace",
            _cols(
                ("workspace_id", "text", ""),
                ("key", "text", ""),
                ("working_ref", "text", "Branch this checkout writes to"),
                ("base", "text", "Manifest hash the working refs were forked from"),
                ("fork_point_json", "json", "State the working branch was forked from"),
                ("last_snapshot_json", "json", "Last fingerprint taken here"),
            ),
            ("workspace_id", "key"),
            "This checkout's working refs (opt-in; moves constantly)",
        ),
    )
}

COMMIT_KEYED = ("commits", "commit_parents", "objects")
"""Immutable facts about one commit; `publish` skips commits already present."""
REPLACED_WHOLESALE = ("refs", "workspace", "tether_meta")
"""Tables that describe *now* and are rewritten on every export / publish."""

SQL_TYPES: dict[str, dict[LogicalType, str]] = {
    "sqlite": {
        "text": "TEXT",
        "int": "INTEGER",
        "bool": "INTEGER",
        "json": "TEXT",
        "timestamp": "TEXT",
    },
    "postgres": {
        "text": "TEXT",
        "int": "BIGINT",
        "bool": "BOOLEAN",
        "json": "JSONB",
        "timestamp": "TIMESTAMPTZ",
    },
}

VIEWS: dict[str, str] = {
    "objects_head": (
        "SELECT o.* FROM {q}objects o JOIN {q}refs r ON r.commit_id = o.commit_id "
        "WHERE r.kind = 'head'"
    ),
    "object_pins": (
        "SELECT o.kind, o.identity_hash, o.pin_id, o.pin_ref, "
        "COUNT(DISTINCT o.commit_id) AS commits FROM {q}objects o "
        "WHERE o.pin_id IS NOT NULL "
        "GROUP BY o.kind, o.identity_hash, o.pin_id, o.pin_ref"
    ),
}
"""Views over the tables; ``{q}`` is the schema qualifier (empty for SQLite)."""


# --------------------------------------------------------------------------- #
# DDL / DML generation (pure functions; tested without a database)
# --------------------------------------------------------------------------- #
def _qualify(schema: str | None) -> str:
    return f"{schema}." if schema else ""


def sqlite_ddl() -> list[str]:
    """`CREATE TABLE IF NOT EXISTS` + views for SQLite."""
    stmts = []
    for t in TABLES.values():
        cols = ", ".join(f"{c.name} {SQL_TYPES['sqlite'][c.type]}" for c in t.columns)
        pk = ", ".join(t.primary_key)
        stmts.append(
            f"CREATE TABLE IF NOT EXISTS {t.name} ({cols}, PRIMARY KEY ({pk}))"
        )
    for name, body in VIEWS.items():
        stmts.append(f"CREATE VIEW IF NOT EXISTS {name} AS {body.format(q='')}")
    return stmts


def postgres_ddl(schema: str = "tether") -> list[str]:
    """`CREATE SCHEMA/TABLE IF NOT EXISTS` + `CREATE OR REPLACE VIEW` for Postgres."""
    _check_identifier(schema)
    q = _qualify(schema)
    stmts = [f"CREATE SCHEMA IF NOT EXISTS {schema}"]
    for t in TABLES.values():
        cols = ", ".join(f"{c.name} {SQL_TYPES['postgres'][c.type]}" for c in t.columns)
        pk = ", ".join(t.primary_key)
        stmts.append(
            f"CREATE TABLE IF NOT EXISTS {q}{t.name} ({cols}, PRIMARY KEY ({pk}))"
        )
    for name, body in VIEWS.items():
        stmts.append(f"CREATE OR REPLACE VIEW {q}{name} AS {body.format(q=q)}")
    return stmts


def _pg_placeholder(col: Column) -> str:
    # Values travel as text; the server casts so the client needs no adapters.
    casts = {"json": "%s::jsonb", "timestamp": "%s::timestamptz", "bool": "%s::boolean"}
    return casts.get(col.type, "%s")


def postgres_upsert_sql(table: str, schema: str = "tether") -> str:
    """Upsert statement for one table.

    `INSERT ... ON CONFLICT (pk) DO UPDATE`; `DO NOTHING` when every column is a key.
    """
    t = TABLES[table]
    q = _qualify(schema)
    cols = ", ".join(t.column_names)
    values = ", ".join(_pg_placeholder(c) for c in t.columns)
    pk = ", ".join(t.primary_key)
    non_key = [c for c in t.column_names if c not in t.primary_key]
    if non_key:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_key)
        action = f"DO UPDATE SET {sets}"
    else:
        action = "DO NOTHING"
    return (
        f"INSERT INTO {q}{table} ({cols}) VALUES ({values}) ON CONFLICT ({pk}) {action}"
    )


def _check_identifier(name: str) -> None:
    if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
        raise ConfigError(f"invalid SQL identifier: {name!r}")


def arrow_schema(table: str) -> Any:
    """pyarrow schema for a table (JSON and timestamps as strings)."""
    import pyarrow as pa

    types = {
        "text": pa.string(),
        "int": pa.int64(),
        "bool": pa.bool_(),
        "json": pa.string(),
        "timestamp": pa.string(),
    }
    return pa.schema([(c.name, types[c.type]) for c in TABLES[table].columns])


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #
@dataclass
class Table:
    """Rows of one export table (dicts keyed by column name; JSON values native)."""

    definition: TableDef
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.definition.name

    def __len__(self) -> int:
        return len(self.rows)

    def _serialized(self, kind: str) -> Iterable[tuple[Any, ...]]:
        """Rows as tuples with JSON dumped and booleans adapted for `kind`."""
        cols = self.definition.columns
        for row in self.rows:
            out = []
            for c in cols:
                v = row.get(c.name)
                if v is None:
                    out.append(None)
                elif c.type == "json":
                    out.append(json.dumps(v, sort_keys=True, separators=(",", ":")))
                elif c.type == "bool":
                    out.append(int(bool(v)) if kind == "sqlite" else bool(v))
                else:
                    out.append(v)
            yield tuple(out)


@dataclass
class PublishReport:
    """Result of `ExportBundle.to_postgres`."""

    schema: str
    upserted: dict[str, int] = field(default_factory=dict)
    """Rows written per table."""
    skipped_commits: int = 0
    """Commits already present in the target and therefore not rewritten."""
    dry_run: bool = False


@dataclass
class ExportBundle:
    """Tables derived from a repository's history, ready to write anywhere."""

    tables: dict[str, Table]
    head: str | None = None
    """Commit id of the working copy at export time."""
    exported_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def __getitem__(self, name: str) -> Table:
        return self.tables[name]

    def row_counts(self) -> dict[str, int]:
        return {name: len(t) for name, t in self.tables.items()}

    # -- SQLite ---------------------------------------------------------- #
    def to_sqlite(self, path: str | os.PathLike[str], *, append: bool = False) -> Path:
        """Write a SQLite database. `append` upserts into an existing file."""
        target = Path(path)
        if not append and target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(target)
        try:
            for stmt in sqlite_ddl():
                con.execute(stmt)
            for name, table in self.tables.items():
                if name in REPLACED_WHOLESALE:
                    con.execute(f"DELETE FROM {name}")
                cols = ", ".join(table.definition.column_names)
                marks = ", ".join("?" for _ in table.definition.columns)
                con.executemany(
                    f"INSERT OR REPLACE INTO {name} ({cols}) VALUES ({marks})",
                    table._serialized("sqlite"),
                )
            con.commit()
        finally:
            con.close()
        return target

    # -- directories of files ------------------------------------------- #
    def to_dir(self, path: str | os.PathLike[str], fmt: str = "jsonl") -> list[Path]:
        """Write one `<table>.<fmt>` per table (plus `schema.json`) into a directory."""
        if fmt not in ("jsonl", "csv", "parquet"):
            raise ConfigError(f"unknown export format {fmt!r}")
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        (out / "schema.json").write_text(json.dumps(schema_document(), indent=2))
        written.append(out / "schema.json")
        for name, table in self.tables.items():
            target = out / f"{name}.{fmt}"
            if fmt == "jsonl":
                with target.open("w", encoding="utf-8") as fh:
                    for row in table.rows:
                        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            elif fmt == "csv":
                with target.open("w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(table.definition.column_names)
                    writer.writerows(table._serialized("csv"))
            else:
                self._write_parquet(table, target)
            written.append(target)
        return written

    def _write_parquet(self, table: Table, target: Path) -> None:
        pa, pq = _pyarrow()
        schema = arrow_schema(table.name)
        rows = [
            dict(zip(schema.names, r, strict=True)) for r in table._serialized("csv")
        ]
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), target)

    def to_arrow(self) -> dict[str, Any]:
        """Every table as a `pyarrow.Table` (needs the `iceberg` or `all` extra)."""
        pa, _ = _pyarrow()
        out = {}
        for name, table in self.tables.items():
            schema = arrow_schema(name)
            rows = [
                dict(zip(schema.names, r, strict=True))
                for r in table._serialized("csv")
            ]
            out[name] = pa.Table.from_pylist(rows, schema=schema)
        return out

    # -- Postgres -------------------------------------------------------- #
    def to_postgres(
        self,
        dsn: str | None = None,
        *,
        schema: str = "tether",
        create: bool = True,
        connection: Any = None,
        dry_run: bool = False,
    ) -> PublishReport:
        """Upsert the tables into a Postgres schema.

        Commits are immutable, so rows of commit-keyed tables whose commit is
        already present are skipped; `refs`, `workspace`, and `tether_meta`
        describe *now* and are rewritten. Everything runs in one transaction.

        Args:
            dsn: libpq connection string (or set `connection`).
            schema: Target schema; created when `create` is true.
            create: Run `CREATE SCHEMA/TABLE IF NOT EXISTS` first.
            connection: An open DB-API connection (tests, pooling); `dsn` is
                ignored when given.
            dry_run: Compute what would be written and return without writing.
        """
        _check_identifier(schema)
        owned = connection is None
        if owned:
            if not dsn:
                raise ConfigError("a Postgres DSN is required to publish")
            connection = _psycopg().connect(dsn)
        report = PublishReport(schema=schema, dry_run=dry_run)
        try:
            cur = connection.cursor()
            existing: set[str] = set()
            if not dry_run and create:
                for stmt in postgres_ddl(schema):
                    cur.execute(stmt)
            try:
                cur.execute(f"SELECT commit_id FROM {schema}.commits")
                existing = {str(r[0]) for r in cur.fetchall()}
            except Exception:
                if not dry_run:
                    raise
                existing = set()  # schema does not exist yet; nothing to skip
                connection.rollback()
                cur = connection.cursor()
            commit_rows = self.tables["commits"].rows
            report.skipped_commits = sum(
                1 for r in commit_rows if r["commit_id"] in existing
            )
            for name, table in self.tables.items():
                rows = table.rows
                if name in COMMIT_KEYED:
                    rows = [r for r in rows if r["commit_id"] not in existing]
                report.upserted[name] = len(rows)
                if dry_run:
                    continue
                if name in REPLACED_WHOLESALE:
                    cur.execute(f"DELETE FROM {schema}.{name}")
                if rows:
                    subset = Table(table.definition, rows)
                    cur.executemany(
                        postgres_upsert_sql(name, schema),
                        list(subset._serialized("pg")),
                    )
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if owned:
                connection.close()
        return report


def schema_document() -> dict[str, Any]:
    """The table definitions as plain data (written beside directory exports)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": {
            t.name: {
                "doc": t.doc,
                "primary_key": list(t.primary_key),
                "columns": [
                    {"name": c.name, "type": c.type, "doc": c.doc} for c in t.columns
                ],
            }
            for t in TABLES.values()
        },
        "views": list(VIEWS),
    }


def _pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ConfigError(
            "Parquet / Arrow export needs pyarrow: pip install 'tether-vcs[iceberg]'"
        ) from exc
    return pa, pq


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise ConfigError(
            "publishing to Postgres needs psycopg: pip install 'tether-vcs[postgres]'"
        ) from exc
    return psycopg


# --------------------------------------------------------------------------- #
# Building rows from a repository
# --------------------------------------------------------------------------- #
def _digest(obj: Any) -> str:
    return _blake(canonical_bytes(obj), size=16)


def _ancestry_depth(infos: Mapping[str, Any]) -> dict[str, int]:
    """Longest parent chain within the exported set (roots and outsiders = 0)."""
    depth: dict[str, int] = {}
    for start in infos:
        stack = [start]
        while stack:
            commit = stack[-1]
            if commit in depth:
                stack.pop()
                continue
            parents = [p for p in infos[commit].parents if p in infos]
            pending = [p for p in parents if p not in depth]
            if pending:
                stack.extend(pending)
                continue
            depth[commit] = 1 + max((depth[p] for p in parents), default=-1)
            stack.pop()
    return depth


def build_bundle(
    repo: Repo,
    *,
    revs: Sequence[str] | None = None,
    listings: bool = False,
    workspace: bool = False,
) -> ExportBundle:
    """Derive the export tables from `repo`.

    Args:
        repo: The repository.
        revs: Revisions to export (jj revsets / git revisions). `None` exports
            every commit reachable in the repository.
        listings: Include `listings` / `listing_entries` from `.tether/listings/`.
        workspace: Include this checkout's working refs and last snapshot.
    """
    tables = {name: Table(defn) for name, defn in TABLES.items()}
    vcs = repo.vcs

    # Manifests per commit.
    per_commit: dict[str, dict[str, ObjectManifest]] = {}
    if revs:
        for rev in revs:
            commit = vcs.resolve(rev)
            per_commit[commit] = repo._objects_at(commit)
    else:
        for commit, objects in repo._iter_history_objects():
            if not commit.strip("0"):
                continue  # jj's virtual root
            per_commit[commit] = objects

    infos = {i.commit_id: i for i in vcs.commit_info(list(per_commit))}
    head = vcs.current_rev()
    depth = _ancestry_depth(infos)

    for commit, objects in per_commit.items():
        info = infos.get(commit)
        if info is None:
            continue  # unreadable commit (should not happen); skip rather than fail
        tables["commits"].rows.append(
            {
                "commit_id": commit,
                "change_id": info.change_id,
                "author_name": info.author_name,
                "author_email": info.author_email,
                "authored_at": info.authored_at,
                "committed_at": info.committed_at,
                "message": info.message,
                "manifest_hash": manifest_hash(objects),
            }
        )
        for pos, parent in enumerate(info.parents):
            tables["commit_parents"].rows.append(
                {"commit_id": commit, "parent_id": parent, "position": pos}
            )

    identities: dict[
        str, tuple[Any, str]
    ] = {}  # (kind, locator json) -> (identity, hash)

    def identity_of(m: ObjectManifest) -> tuple[Any, str]:
        cache_key = f"{m.kind}|{canonical_bytes(m.locator).decode()}"
        hit = identities.get(cache_key)
        if hit is None:
            try:
                ident: Any = repo.backend_for(m.kind).identity(m.locator)
            except TetherError:
                ident = None  # backend unavailable here; identity stays unknown
            hit = (ident, _digest(ident) if ident is not None else "")
            identities[cache_key] = hit
        return hit

    states: dict[tuple[str, str, str], dict[str, Any]] = {}
    for commit, objects in per_commit.items():
        info = infos.get(commit)
        if info is None:
            continue
        for key in sorted(objects):
            m = objects[key]
            ident, ident_hash = identity_of(m)
            state_hash = _digest(m.state) if m.state is not None else None
            tables["objects"].rows.append(
                {
                    "commit_id": commit,
                    "key": key,
                    "kind": m.kind,
                    "uri": m.locator.get("uri"),
                    "locator_json": dict(m.locator),
                    "identity_json": ident,
                    "identity_hash": ident_hash or None,
                    "policy_write": m.policy.write,
                    "policy_file": m.policy.file,
                    "policy_pin": m.policy.pin,
                    "state_json": m.state,
                    "state_hash": state_hash,
                    "pin_id": m.pin.id if m.pin else None,
                    "pin_ref": m.pin.ref if m.pin else None,
                    "recoverable": m.recoverable,
                    "captured_at": m.captured_at,
                }
            )
            if m.state is None or state_hash is None:
                continue
            skey = (m.kind, ident_hash, state_hash)
            prev = states.get(skey)
            # "First" = shallowest in the ancestry graph; timestamps only break
            # ties between unrelated commits (many land within one second).
            rank = (depth.get(commit, 0), info.committed_at, commit)
            if prev is None or rank < prev["_rank"]:
                states[skey] = {
                    "_rank": rank,
                    "kind": m.kind,
                    "identity_hash": ident_hash or None,
                    "state_hash": state_hash,
                    "identity_json": ident,
                    "state_json": m.state,
                    "pin_id": m.pin.id if m.pin else (prev or {}).get("pin_id"),
                    "first_commit_id": commit,
                    "first_committed_at": info.committed_at,
                }
    for k in sorted(states):
        row = dict(states[k])
        row.pop("_rank")
        tables["object_states"].rows.append(row)

    exported = {r["commit_id"] for r in tables["commits"].rows}
    for ref in vcs.refs():
        if ref.commit_id in exported:
            tables["refs"].rows.append(
                {"name": ref.name, "kind": ref.kind, "commit_id": ref.commit_id}
            )

    if listings:
        _add_listings(repo, tables, per_commit, identity_of)

    if workspace:
        ws = repo.workspace
        for key in sorted(set(ws.working_refs) | set(ws.last_snapshot)):
            tables["workspace"].rows.append(
                {
                    "workspace_id": ws.workspace_id,
                    "key": key,
                    "working_ref": ws.working_refs.get(key),
                    "base": ws.base,
                    "fork_point_json": ws.fork_points.get(key),
                    "last_snapshot_json": ws.last_snapshot.get(key),
                }
            )

    bundle = ExportBundle(tables=tables, head=head)
    from tether import __version__

    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "exported_at": bundle.exported_at,
        "head": head,
        "vcs": vcs.kind,
        "tether_version": __version__,
    }
    tables["tether_meta"].rows.extend({"key": k, "value": v} for k, v in meta.items())
    return bundle


def _add_listings(
    repo: Repo,
    tables: dict[str, Table],
    per_commit: Mapping[str, Mapping[str, ObjectManifest]],
    identity_of: Any,
) -> None:
    """Rows for every listing file a manifest names and the working tree still has."""
    seen: set[str] = set()
    for objects in per_commit.values():
        for m in objects.values():
            if m.state is None:
                continue
            ident, ident_hash = identity_of(m)
            if ident is None:
                continue
            name = listing_name(m.kind, ident, m.state)
            if name in seen:
                continue
            seen.add(name)
            text = read_listing(repo.root, name)
            if text is None:
                continue
            count = 0
            for line in text.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                tables["listing_entries"].rows.append(
                    {
                        "listing_name": name,
                        "path": str(row.get("p", "")),
                        "token": str(row.get("k", "")) or None,
                        "size": int(row.get("s", 0)),
                    }
                )
                count += 1
            tables["listings"].rows.append(
                {
                    "listing_name": name,
                    "kind": m.kind,
                    "identity_hash": ident_hash,
                    "state_hash": _digest(m.state),
                    "entries": count,
                }
            )
