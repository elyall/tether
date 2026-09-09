"""The operation log: what tether did to the stores, per workspace.

The enclosing jj/git repository records the *manifests* -- what each commit
says about every object. It cannot see what tether did to reach that point:
which native refs `commit` created, which branches `new` forked or reset (and
what their heads were before), what `gc` deleted, where `promote` moved a base
branch from. This log does, so that `undo` can reverse an operation where the
store still allows it, `repair` can rebuild what a manifest promises, and a
half-applied operation is visible rather than silent.

Like jj's own operation log it is per working copy and never shared:
``.tether/ops.jsonl``, ignored by the VCS, one JSON object per line, append
only (an undo appends a new entry and marks its target ``undone_by``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tether.manifest import tether_path

OPS_FILENAME = "ops.jsonl"
"""The log file inside ``.tether/`` (untracked)."""

__all__ = [
    "OPS_FILENAME",
    "OpEntry",
    "append_op",
    "mark_undone",
    "new_op_id",
    "ops_path",
    "read_ops",
]


def new_op_id() -> str:
    """A fresh 12-hex operation id."""
    return uuid.uuid4().hex[:12]


def ops_path(root: Path) -> Path:
    return tether_path(root) / OPS_FILENAME


@dataclass
class OpEntry:
    """One applied operation.

    Attributes:
        id: 12-hex identifier (`tether undo ID`).
        at: When it was applied (UTC, ISO 8601).
        command: `commit`, `new`, `fork`, `gc`, `promote`, `import`, `add`,
            `remove`, `repair`, or `undo`.
        plan: The `Plan` that was applied, as a dict (`None` for direct ops).
        result: What happened: pins created, branches forked, the VCS commit,
            the report -- whatever the command produced.
        pre: What `undo` needs to reverse it: workspace state before, VCS
            position before, branch heads before they were reset, manifests
            before they were rewritten.
        undoes: For an `undo` entry, the id of the operation it reversed.
        undone_by: Set on an entry once an `undo` has reversed it.
    """

    id: str
    at: str
    command: str
    plan: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    pre: dict[str, Any] = field(default_factory=dict)
    undoes: str | None = None
    undone_by: str | None = None

    @classmethod
    def now(
        cls,
        command: str,
        *,
        plan: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        pre: dict[str, Any] | None = None,
        undoes: str | None = None,
    ) -> OpEntry:
        return cls(
            id=new_op_id(),
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            command=command,
            plan=plan,
            result=dict(result or {}),
            pre=dict(pre or {}),
            undoes=undoes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "command": self.command,
            "plan": self.plan,
            "result": self.result,
            "pre": self.pre,
            "undoes": self.undoes,
            "undone_by": self.undone_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpEntry:
        return cls(
            id=str(data["id"]),
            at=str(data.get("at", "")),
            command=str(data["command"]),
            plan=data.get("plan"),
            result=dict(data.get("result") or {}),
            pre=dict(data.get("pre") or {}),
            undoes=data.get("undoes"),
            undone_by=data.get("undone_by"),
        )

    @property
    def undoable(self) -> bool:
        """Not itself an undo, not already undone, and of a reversible kind."""
        return (
            self.undoes is None
            and self.undone_by is None
            and self.command
            not in ("undo", "repair", "upgrade", "abandon", "forget-workspace")
        )

    def summary(self) -> str:
        """One line for `tether ops`."""
        r = self.result
        if self.command == "commit":
            pinned = [k for k, v in (r.get("pinned") or {}).items() if v]
            recorded = [k for k, v in (r.get("pinned") or {}).items() if not v]
            bits = []
            if pinned:
                bits.append(f"pinned {', '.join(sorted(pinned))}")
            if recorded:
                bits.append(f"recorded {', '.join(sorted(recorded))}")
            if r.get("vcs_commit"):
                bits.append(f"vcs {str(r['vcs_commit'])[:12]}")
            return "; ".join(bits) or "nothing changed"
        if self.command == "new":
            bits = []
            if r.get("created"):
                bits.append(f"forked {', '.join(sorted(r['created']))}")
            if r.get("reset"):
                bits.append(f"reset {', '.join(sorted(r['reset']))}")
            if r.get("reused"):
                bits.append(f"kept {', '.join(sorted(r['reused']))}")
            if r.get("pending_forks"):
                bits.append(f"deferred {', '.join(sorted(r['pending_forks']))}")
            rev = ((self.plan or {}).get("context") or {}).get("rev")
            if rev:
                bits.append(f"at {rev}")
            return "; ".join(bits) or "working refs unchanged"
        if self.command == "restore":
            keys = sorted({*(r.get("created") or []), *(r.get("reset") or [])})
            return f"restored {', '.join(keys)} from {str(r.get('from_commit'))[:12]}"
        if self.command == "fork":
            return f"forked {r.get('key')} -> {r.get('ref')}"
        if self.command == "gc":
            n_pins = sum(len(v) for v in (r.get("unpinned") or {}).values())
            n_br = sum(len(v) for v in (r.get("deleted_working_refs") or {}).values())
            return f"unpinned {n_pins}, deleted {n_br} branch(es)"
        if self.command == "promote":
            keys = sorted({*(r.get("fast_forwarded") or {}), *(r.get("merged") or {})})
            return f"promoted {', '.join(keys)}" if keys else "nothing promoted"
        if self.command == "import":
            return (
                f"added {len(r.get('added') or [])}, updated "
                f"{len(r.get('updated') or [])}, removed {len(r.get('removed') or [])}"
            )
        if self.command in ("add", "remove"):
            return str(r.get("key", ""))
        if self.command == "undo":
            return f"undid {self.undoes}: {r.get('summary', '')}"
        if self.command == "repair":
            return (
                f"repinned {len(r.get('repinned') or [])}, reforked "
                f"{len(r.get('reforked') or [])}"
            )
        if self.command == "forget-workspace":
            return f"forgot workspace {r.get('workspace')}"
        if self.command == "abandon":
            ids = ", ".join(str(c)[:12] for c in r.get("abandoned") or [])
            return f"abandoned {ids}; {len(r.get('unreferenced') or [])} pin(s) freed"
        if self.command == "upgrade":
            return (
                f"v{r.get('from_version')} -> v{r.get('to_version')}: "
                f"{len(r.get('renamed_pins') or {})} pin(s), "
                f"{len(r.get('renamed_branches') or {})} branch(es), "
                f"{len(r.get('rewritten_commits') or {})} commit(s) rewritten"
            )
        return ""


def read_ops(root: Path) -> list[OpEntry]:
    """Every entry, oldest first (a missing or empty log is an empty list)."""
    path = ops_path(root)
    if not path.is_file():
        return []
    entries: list[OpEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(OpEntry.from_dict(json.loads(line)))
    return entries


def append_op(root: Path, entry: OpEntry) -> None:
    path = ops_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), default=str) + "\n")


def mark_undone(root: Path, op_id: str, by: str) -> None:
    """Record that `op_id` was reversed by the undo entry `by`."""
    entries = read_ops(root)
    for e in entries:
        if e.id == op_id:
            e.undone_by = by
    path = ops_path(root)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        "".join(json.dumps(e.to_dict(), default=str) + "\n" for e in entries),
        encoding="utf-8",
    )
    tmp.replace(path)
