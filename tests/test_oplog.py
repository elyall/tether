"""The op log file: append-only entries and marks."""

from __future__ import annotations

from pathlib import Path

from tether.oplog import OpEntry, append_op, mark_undone, ops_path, read_ops


def test_marks_are_appended_not_rewritten(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".tether").mkdir()
    a = OpEntry.now("commit", result={"vcs_commit": "abc"})
    b = OpEntry.now("new", result={"created": ["db"]})
    append_op(root, a)
    append_op(root, b)
    before = ops_path(root).read_text()

    mark_undone(root, b.id, "undo0001")
    after = ops_path(root).read_text()
    assert after.startswith(before)  # nothing before the mark was touched
    assert after[len(before) :].strip() == f'{{"undone": "{b.id}", "by": "undo0001"}}'

    entries = read_ops(root)
    assert [e.id for e in entries] == [a.id, b.id]
    assert entries[1].undone_by == "undo0001" and entries[0].undone_by is None
    assert not entries[1].undoable and entries[0].undoable

    # A torn last line (crash mid-append) is skipped, not fatal.
    with ops_path(root).open("a") as fh:
        fh.write('{"command": "gc", "id": "trunc')
    assert [e.id for e in read_ops(root)] == [a.id, b.id]
    # A mark for an unknown id is harmless.
    mark_undone(root, "nope", "undo0002")
    assert [e.undone_by for e in read_ops(root)] == [None, "undo0001"]
