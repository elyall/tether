"""Shim: moved to :mod:`tether.experimental.backends.ducklake` in 0.1.0b1.

This import path is removed at 0.2. `tether add --kind ducklake` is unaffected.
"""

from __future__ import annotations

import warnings

from tether.experimental.backends.ducklake import *  # noqa: F403
from tether.experimental.backends.ducklake import (
    DuckLakeBackend,
    _factory,
)

warnings.warn(
    "tether.backends.ducklake moved to tether.experimental.backends.ducklake; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["DuckLakeBackend", "_factory"]
