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
only. Two kinds of line: an entry (has ``"command"``) and a mark
(``{"undone": <id>, "by": <undo id>}``), which is how an undo records that it
reversed an earlier entry without rewriting the file -- a crash between two
appends loses at most the line being written, never the log.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
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
    "mark_done",
    "mark_progress",
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


def report_dict(report: Any) -> dict[str, Any]:
    """A report dataclass as plain data for the op log (without its plan).

    Every `apply_*` -- in the core, in `tether.upgrade`, in
    `tether.experimental.registry` -- records its report this way, so the
    helper lives with the log rather than as a private name in `repo.py`.
    """
    data = dataclasses.asdict(report)
    data.pop("plan", None)
    return data


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
        status: `"started"` while the operation runs -- the entry is appended
            (and synced) *before* the first side effect, with the plan and what
            `undo` would need -- and `"done"` once a completion mark with the
            result follows. An entry still `"started"` in a log nobody is
            writing to is an operation that was interrupted: `ops` flags it,
            `undo` refuses it (what happened is not known), `repair` names it.
    """

    id: str
    at: str
    command: str
    plan: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    pre: dict[str, Any] = field(default_factory=dict)
    undoes: str | None = None
    undone_by: str | None = None
    status: str = "done"
    progress: list[dict[str, Any]] = field(default_factory=list)
    """Per-action progress records appended while the operation ran (each a
    dict with at least `action`): what an interrupted operation *did* get
    done. Read from the log; not part of the entry's own record."""

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

    @property
    def incomplete(self) -> bool:
        """The operation began but no completion mark followed."""
        return self.status == "started"

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
            "status": self.status,
            "progress": list(self.progress),
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
            status=str(data.get("status") or "done"),
        )

    @property
    def undoable(self) -> bool:
        """Not itself an undo, not already undone, complete, and of a
        reversible kind."""
        return (
            self.undoes is None
            and self.undone_by is None
            and not self.incomplete
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
            text = f"unpinned {n_pins}, deleted {n_br} branch(es)"
            n_stores = len(r.get("deleted_stores") or {})
            if n_stores:
                text += f", deleted {n_stores} store(s)"
            return text
        if self.command == "promote":
            keys = sorted({*(r.get("fast_forwarded") or {}), *(r.get("merged") or {})})
            return f"promoted {', '.join(keys)}" if keys else "nothing promoted"
        if self.command == "import":
            return (
                f"added {len(r.get('added') or [])}, updated "
                f"{len(r.get('updated') or [])}, removed {len(r.get('removed') or [])}"
            )
        if self.command in ("add", "remove"):
            key = str(r.get("key", ""))
            return f"{key} (created store)" if r.get("created") else key
        if self.command == "set":
            return ", ".join(
                f"{k}: " + " ".join(f"{f}={v[0]}->{v[1]}" for f, v in d.items())
                for k, d in (r.get("changed") or {}).items()
            )
        if self.command == "pull":
            pinned = r.get("pinned") or {}
            return (
                f"pulled {len(pinned)} object(s) -> {str(r.get('vcs_commit', ''))[:12]}"
            )
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
    """Every entry, oldest first, with undo marks applied (missing log: empty)."""
    path = ops_path(root)
    if not path.is_file():
        return []
    entries: list[OpEntry] = []
    marks: dict[str, str] = {}
    done: dict[str, dict[str, Any]] = {}
    progress: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from an interrupted append
        if "command" in obj:
            entries.append(OpEntry.from_dict(obj))
        elif "done" in obj:
            done[str(obj["done"])] = obj
            if obj.get("undone"):
                marks[str(obj["undone"])] = str(obj.get("by", ""))
        elif "undone" in obj:
            marks[str(obj["undone"])] = str(obj.get("by", ""))
        elif "progress" in obj:
            record = {k: v for k, v in obj.items() if k != "progress"}
            progress.setdefault(str(obj["progress"]), []).append(record)
    for e in entries:
        e.progress = progress.get(e.id, [])
        if e.id in marks:
            e.undone_by = marks[e.id] or None
        if e.id in done:
            mark = done[e.id]
            e.status = "done"
            e.result = dict(mark.get("result") or e.result)
            if mark.get("pre"):
                e.pre = {**e.pre, **dict(mark["pre"])}
    return entries


_APPEND_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Repository-wide indexes (beside the repository lock in the VCS's shared dir)
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every decodable record of a jsonl index, oldest first. A line an
    interrupted append left torn is skipped, as `read_ops` skips one: an
    index is a memory aid, and a damaged line must never fail the command
    that happens to read it."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def append_index_entry(path: Path, data: dict[str, Any]) -> None:
    """Append one record and sync it (one `write` of one line, O_APPEND).

    A torn final line -- an earlier append that was interrupted -- has no
    newline; the new record starts on a fresh line so the fragment stays a
    fragment (skipped by `read_jsonl`) instead of swallowing this record too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = ""
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                lead = "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(lead + json.dumps(data, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def remove_index_entry(path: Path, dataset_id: str, identity: dict[str, Any]) -> None:
    if not path.is_file():
        return
    with _APPEND_LOCK:
        keep = [
            d
            for d in read_jsonl(path)
            if not (
                d.get("dataset_id") == dataset_id
                and dict(d.get("identity") or {}) == identity
            )
        ]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "".join(json.dumps(d, default=str) + "\n" for d in keep), encoding="utf-8"
        )
        tmp.replace(path)


TOUCHED_FILENAME = "tether-touched.jsonl"
"""Repository-wide index of stores this clone has *written refs into* -- forked
a working branch or created a pin -- kept beside `tether-created.jsonl`. The
ordinary `gc` only looks inside stores some manifest in history names; a
store this clone forked into on a bookmark it then abandoned is named by no
manifest any more, and its dead refs would stay forever (and keep the store's
creator from reclaiming it). This index tells `gc` where else to look.
"""


@dataclass(frozen=True)
class TouchedStore:
    """One store this clone forked or pinned in (`gc` releases its own dead
    refs there even when no manifest names the store any more)."""

    dataset_id: str
    kind: str
    identity: dict[str, Any]
    locator: dict[str, Any]
    key: str
    """The key the object had when the store was touched (informational)."""
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "identity": dict(self.identity),
            "locator": dict(self.locator),
            "key": self.key,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TouchedStore:
        return cls(
            dataset_id=str(data["dataset_id"]),
            kind=str(data["kind"]),
            identity=dict(data.get("identity") or {}),
            locator=dict(data.get("locator") or {}),
            key=str(data.get("key", "")),
            at=str(data.get("at", "")),
        )


def touched_path(shared_dir: Path) -> Path:
    return shared_dir / TOUCHED_FILENAME


def append_touched(shared_dir: Path, entry: TouchedStore) -> bool:
    """Record a store this clone just wrote a ref into; a set, so an identity
    already on record is not written again. Returns whether it was new."""
    path = touched_path(shared_dir)
    with _APPEND_LOCK:
        for d in read_jsonl(path):
            if (
                d.get("dataset_id") == entry.dataset_id
                and dict(d.get("identity") or {}) == entry.identity
            ):
                return False
        append_index_entry(path, entry.to_dict())
    return True


def read_touched(shared_dir: Path, dataset_id: str | None = None) -> list[TouchedStore]:
    """Every touched store on record (optionally one dataset's), oldest first."""
    out: list[TouchedStore] = []
    for data in read_jsonl(touched_path(shared_dir)):
        entry = TouchedStore.from_dict(data)
        if dataset_id is None or entry.dataset_id == dataset_id:
            out.append(entry)
    return out


def remove_touched(shared_dir: Path, dataset_id: str, identity: dict[str, Any]) -> None:
    """Forget a store (nothing of this dataset's is left in it)."""
    remove_index_entry(touched_path(shared_dir), dataset_id, identity)


def _append_line(root: Path, obj: dict[str, Any]) -> None:
    """Append one JSON line and sync it: the log is a journal, and a started
    entry must survive whatever interrupts the operation after it. Progress
    records are appended from fan-out threads, hence the lock."""
    path = ops_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def mark_progress(root: Path, op_id: str, action: str, **detail: Any) -> None:
    """Record that one action of a running operation completed.

    A multi-action operation (a `new` forking five branches, a `gc` releasing
    twenty pins) that dies half-way leaves a started entry; these records say
    which of its actions had already taken effect, so `repair` can list them
    and a re-run knows what is left.
    """
    _append_line(root, {"progress": op_id, "action": action, **detail})


def append_op(root: Path, entry: OpEntry) -> None:
    _append_line(root, entry.to_dict())


def mark_done(
    root: Path,
    op_id: str,
    result: dict[str, Any],
    pre: dict[str, Any] | None = None,
    *,
    undone: str | None = None,
) -> None:
    """Complete a started entry: what happened, and anything `undo` learnt
    only while the operation ran (branch heads it replaced, for one). An undo
    names the entry it reversed in the same record, so completing the undo
    and marking its target undone is one append -- there is no state where
    the undo is done but its target still counts as undoable."""
    line: dict[str, Any] = {"done": op_id, "result": result, "pre": dict(pre or {})}
    if undone is not None:
        line["undone"] = undone
        line["by"] = op_id
    _append_line(root, line)


def mark_undone(root: Path, op_id: str, by: str) -> None:
    """Record that `op_id` was reversed by the undo entry `by` (an appended mark)."""
    _append_line(root, {"undone": op_id, "by": by})
