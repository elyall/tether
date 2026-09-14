"""Shim: moved to :mod:`tether.experimental.registry.registry` in 0.1.0b1.

This import path is removed at 0.2; import the public names from `tether`.
"""

from __future__ import annotations

import warnings

from tether.experimental.registry.registry import *  # noqa: F403

warnings.warn(
    "tether.registry moved to tether.experimental.registry.registry; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)
