"""Shim: moved to :mod:`tether.experimental.backends.dolt` in 0.1.0b1.

This import path is removed at 0.2. `tether add --kind dolt` is unaffected.
"""

from __future__ import annotations

import warnings

from tether.experimental.backends.dolt import *  # noqa: F403
from tether.experimental.backends.dolt import (
    DoltBackend,
    _factory,
)

warnings.warn(
    "tether.backends.dolt moved to tether.experimental.backends.dolt; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["DoltBackend", "_factory"]
