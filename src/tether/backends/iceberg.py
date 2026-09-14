"""Shim: moved to :mod:`tether.experimental.backends.iceberg` in 0.1.0b1.

This import path is removed at 0.2. `tether add --kind iceberg` is unaffected.
"""

from __future__ import annotations

import warnings

from tether.experimental.backends.iceberg import *  # noqa: F403
from tether.experimental.backends.iceberg import (
    IcebergBackend,
    _factory,
)

warnings.warn(
    "tether.backends.iceberg moved to tether.experimental.backends.iceberg; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["IcebergBackend", "_factory"]
