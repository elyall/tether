"""Backend protocol, capability tiers, reports, and the registry.

A backend maps tether's operations onto one class of system. Built-in kinds
(`file`, `icechunk`, `neon`, `git`, `iceberg`, `delta`, `lance`, `lakefs`,
`ducklake`, `dolt`, `memory`) live in `tether.backends.<kind>` and register
themselves on import; `build_backend` imports them lazily so importing this
package does not pull in optional third-party dependencies. Third-party
backends implement `ObjectBackend` and call `register_backend`.
"""

from __future__ import annotations

from tether.backends.base import (
    MAX_DIFF_ENTRIES,
    Capability,
    ChangeEntry,
    HistoryEntry,
    Listings,
    ObjectBackend,
    ObjectDiff,
    Tier,
    VerifyReport,
    VerifyStatus,
    base_at,
    build_backend,
    content_state,
    effective_capabilities,
    known_kinds,
    register_backend,
    tier_of,
)

__all__ = [
    "MAX_DIFF_ENTRIES",
    "Capability",
    "ChangeEntry",
    "HistoryEntry",
    "Listings",
    "ObjectBackend",
    "ObjectDiff",
    "Tier",
    "VerifyReport",
    "VerifyStatus",
    "base_at",
    "build_backend",
    "content_state",
    "effective_capabilities",
    "known_kinds",
    "register_backend",
    "tier_of",
]
