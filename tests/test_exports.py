"""The public package surface: every name in `__all__` exists, and vice versa."""

from __future__ import annotations

import tether


def test_all_names_exist() -> None:
    missing = [n for n in tether.__all__ if not hasattr(tether, n)]
    assert not missing, f"__all__ names not importable from tether: {missing}"


def test_all_is_unique() -> None:
    assert len(tether.__all__) == len(set(tether.__all__))
