"""Registry -> tether: derive the tracked object set from a SQL (or file) source.

A registry knows *what data exists and where*. `tether import` reads rows in
tether's canonical object columns from it -- the mapping from registry columns
is written in SQL on the registry side -- and turns them into `add` / `update`
/ `remove` actions on the working-tree manifests. Nothing external is touched
and nothing is pinned; `tether commit` still records states.

The canonical columns are a subset of the export `objects` table, so
`export -> edit -> import` round-trips.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tether.backends.base import known_kinds
from tether.errors import ConfigError
from tether.manifest import Locator, Policy, key_to_relpath

__all__ = ["CANONICAL_COLUMNS", "ImportSpec", "read_source", "specs_from_rows"]

CANONICAL_COLUMNS: tuple[str, ...] = (
    "key",
    "kind",
    "uri",
    "locator_json",
    "policy_write",
    "policy_file",
    "policy_pin",
    "at",
)
"""Columns `import` understands. `key` and `kind` are required; the rest optional."""

_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


@dataclass(frozen=True)
class ImportSpec:
    """One desired object: what a registry row asks tether to track."""

    key: str
    kind: str
    locator: Locator
    policy: Policy

    @classmethod
    def from_row(cls, row: Mapping[str, Any], defaults: Policy) -> ImportSpec:
        """Validate one canonical row.

        Raises:
            ConfigError: Missing `key`/`kind`, unknown kind, unsafe key, bad
                policy value, or `locator_json` that is not a JSON object.
        """
        key = str(row.get("key") or "").strip()
        kind = str(row.get("kind") or "").strip()
        if not key or not kind:
            raise ConfigError(f"import row needs `key` and `kind`: {dict(row)!r}")
        key_to_relpath(key)  # raises on unsafe keys
        if kind not in known_kinds():
            raise ConfigError(f"unknown backend kind {kind!r} for {key!r}")

        locator: dict[str, Any] = {}
        raw = row.get("locator_json")
        if raw not in (None, ""):
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, Mapping):
                raise ConfigError(f"locator_json for {key!r} must be a JSON object")
            locator.update({str(k): v for k, v in parsed.items() if v is not None})
        uri = row.get("uri")
        if uri not in (None, "") and "uri" not in locator:
            locator["uri"] = str(uri)
        at = row.get("at")
        if at not in (None, ""):
            locator["at"] = str(at)
        if not locator:
            raise ConfigError(
                f"import row for {key!r} has no locator (uri/locator_json)"
            )

        policy_data = {
            "write": row.get("policy_write") or defaults.write,
            "file": row.get("policy_file") or defaults.file,
            "pin": row.get("policy_pin") or defaults.pin,
        }
        policy = Policy.from_dict(policy_data)
        return cls(key=key, kind=kind, locator=locator, policy=policy)


def specs_from_rows(
    rows: Iterable[Mapping[str, Any]], defaults: Policy
) -> tuple[list[ImportSpec], list[str]]:
    """Validate rows into specs.

    Returns `(specs, notes)`; notes name any columns that were ignored.
    """
    specs: list[ImportSpec] = []
    notes: list[str] = []
    seen: set[str] = set()
    extra: set[str] = set()
    for row in rows:
        extra.update(c for c in row if c not in CANONICAL_COLUMNS)
        spec = ImportSpec.from_row(row, defaults)
        if spec.key in seen:
            raise ConfigError(f"import source lists {spec.key!r} more than once")
        seen.add(spec.key)
        specs.append(spec)
    if extra:
        notes.append(f"ignored columns: {', '.join(sorted(extra))}")
    return specs, notes


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def read_source(
    source: str,
    *,
    table: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Read canonical rows from a registry.

    `source` is a Postgres DSN (`postgresql://...`), a SQLite file, a `.csv`, or
    a `.jsonl` / `.ndjson` file. SQL sources need `--table` or `--query`.
    """
    if source.startswith(_POSTGRES_SCHEMES):
        return _read_postgres(source, table, query)
    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    if suffix in (".jsonl", ".ndjson"):
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
    if not path.is_file():
        raise ConfigError(f"import source not found: {source}")
    return _read_sqlite(path, table, query)


def _select(table: str | None, query: str | None) -> str:
    if bool(table) == bool(query):
        raise ConfigError("give exactly one of --table or --query for a SQL source")
    if query:
        return query
    assert table is not None
    if not table.replace("_", "a").replace(".", "a").isalnum():
        raise ConfigError(f"invalid table name: {table!r}")
    return f"SELECT * FROM {table}"


def _read_sqlite(
    path: Path, table: str | None, query: str | None
) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(_select(table, query)).fetchall()]
    finally:
        con.close()


def _read_postgres(
    dsn: str, table: str | None, query: str | None
) -> list[dict[str, Any]]:
    try:
        import psycopg
    except ImportError as exc:
        raise ConfigError(
            "importing from Postgres needs psycopg: pip install 'tether-vcs[postgres]'"
        ) from exc
    # Plain DB-API usage; the query text is user-supplied, so no static typing.
    conn: Any = psycopg.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(_select(table, query))
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    finally:
        conn.close()
