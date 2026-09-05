"""Built-in backends.

Backends register themselves on import. :func:`tether.backends.base.build_backend`
imports the built-in modules lazily, so importing this package does not pull in
optional third-party dependencies.
"""

from __future__ import annotations

from tether.backends.base import (
    Capability,
    ObjectBackend,
    Tier,
    VerifyReport,
    VerifyStatus,
    build_backend,
    known_kinds,
    register_backend,
    tier_of,
)

__all__ = [
    "Capability",
    "ObjectBackend",
    "Tier",
    "VerifyReport",
    "VerifyStatus",
    "build_backend",
    "known_kinds",
    "register_backend",
    "tier_of",
]
