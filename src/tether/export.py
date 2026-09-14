"""Shim: moved to :mod:`tether.experimental.registry.export` in 0.1.0b1.

This import path is removed at 0.2; import the public names from `tether`.
"""

from __future__ import annotations

import warnings

from tether.experimental.registry.export import *  # noqa: F403

warnings.warn(
    "tether.export moved to tether.experimental.registry.export; "
    "this import path is removed at 0.2",
    DeprecationWarning,
    stacklevel=2,
)
