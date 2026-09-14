"""Shim: moved to :mod:`tether.experimental.backends.lakefs` in 0.1.0b1.

This import path is removed at 0.2. `tether add --kind lakefs` is unaffected.
"""

from __future__ import annotations

import warnings

from tether.experimental.backends.lakefs import *  # noqa: F403
from tether.experimental.backends.lakefs import LakeFSBackend, _factory

warnings.warn(
    "tether.backends.lakefs moved to tether.experimental.backends.lakefs; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["LakeFSBackend", "_factory"]
