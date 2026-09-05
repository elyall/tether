from __future__ import annotations

from pathlib import Path

from tether.vcs import detect_vcs


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_adapter_roundtrip(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)

    _write(vcs_root, ".tether/objects/a.toml", "key='a'\n")
    _write(vcs_root, "tether.toml", "v=1\n")
    c1 = vcs.commit([".tether", "tether.toml"], "first")
    assert c1 and vcs.resolve(c1) == c1

    # Read the file back at the commit.
    text = vcs.read_file_at(c1, ".tether/objects/a.toml")
    assert text is not None and "key='a'" in text
    assert vcs.read_file_at(c1, ".tether/objects/missing.toml") is None

    files = vcs.list_files_at(c1, ".tether/objects")
    assert ".tether/objects/a.toml" in files

    # A second commit shows up in history.
    _write(vcs_root, ".tether/objects/b.toml", "key='b'\n")
    c2 = vcs.commit([".tether", "tether.toml"], "second")
    assert c2 != c1
    history = vcs.history_revs()
    assert c1 in history and c2 in history

    b_at_c2 = vcs.list_files_at(c2, ".tether/objects")
    assert ".tether/objects/b.toml" in b_at_c2
    assert ".tether/objects/b.toml" not in vcs.list_files_at(c1, ".tether/objects")


def test_detect_prefers_jj_when_colocated(vcs_root: Path) -> None:
    vcs = detect_vcs(vcs_root)
    assert vcs.kind in ("git", "jj")
    assert Path(vcs.root) == vcs_root
