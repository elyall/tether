"""Shim: moved to :mod:`tether.experimental.backends.neon` in 0.1.0b1.

This import path is removed at 0.2. `tether add --kind neon` is unaffected.
"""

from __future__ import annotations

import warnings

from tether.experimental.backends.neon import *  # noqa: F403
from tether.experimental.backends.neon import (
    NeonBackend,
    _factory,
)

warnings.warn(
    "tether.backends.neon moved to tether.experimental.backends.neon; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["NeonBackend", "_factory"]
