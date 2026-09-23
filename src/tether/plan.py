"""Plans: what a store-writing command *would* do, as data.

The planned commands (`commit` pins, `new` forks, `gc` unpins / deletes
branches, and `restore`, `promote`, `drop`, `forget-workspace`, `import`,
`repair`, `upgrade`) are split into a read-only *plan* step and an *apply*
step; `pull`, `undo`, `abandon --gc` and `add --create` write without one.
A `Plan` lists the concrete `Action`s with the inputs they were
computed from, serializes to JSON (`tether commit --dry-run --json`), and can be
applied later (`tether commit --from-plan plan.json`); apply re-checks that the
world still matches the plan before touching anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tether.errors import ConfigError, StalePlanError

PLAN_FORMAT = 3
"""Bumped when a saved plan's shape changes. Format 1 (before 0.1.0b1) had
no `preconditions`; format 2 (before 0.1.0b4) no `digest`, so its actions
and context could be edited apart from the preconditions that vouch for
them. Either is refused with "re-run the plan"."""

_UNBOUND_FORMATS = {
    1: "plan format 1 predates 0.1.0b1 and carries no preconditions",
    2: "plan format 2 predates 0.1.0b4 and carries no digest binding its actions "
    "and context to its preconditions",
}

PRECONDITION_KINDS = frozenset(
    {
        "manifest_hash",
        "workspace_id",
        "workspace_bookmark",
        "vcs_head",
        "history_digest",
        "config_version",
        "ref_absent",
        "ref_head",
        "base_state",
        "pin_state",
        "no_new_holders",
        "store_empty",
        "bookmark_head",
    }
)
"""What a plan may require of the world before it is applied. Each is one
check `Repo._verify_plan` knows how to run; a `plan_*` appends them, and
every `apply_*` runs them all before its first action. `workspace_bookmark`
is the bookmark the checkout works on, as `workspace.toml` records it *and*
as the VCS places the working copy (git's `HEAD` branch; under jj the
bookmarks at `@` or, with edits in `@`, at `@-`)."""

REQUIRED_PRECONDITIONS: dict[str, frozenset[str]] = {
    "commit": frozenset({"workspace_id", "workspace_bookmark", "manifest_hash"}),
    "new": frozenset({"workspace_id", "manifest_hash"}),
    "restore": frozenset({"workspace_id", "workspace_bookmark", "manifest_hash"}),
    "promote": frozenset(
        {"workspace_id", "workspace_bookmark", "bookmark_head", "manifest_hash"}
    ),
    "drop": frozenset(
        {
            "workspace_id",
            "workspace_bookmark",
            "bookmark_head",
            "no_new_holders",
            "vcs_head",
            "history_digest",
        }
    ),
    "gc": frozenset({"workspace_id", "history_digest"}),
    "repair": frozenset({"workspace_id", "history_digest"}),
}
"""The preconditions a command's plan must carry whatever it found to do.

A plan supplies its own preconditions, so a plan that lacks one -- saved by a
tether from before the kind existed, or edited -- would apply wherever and
whenever it was loaded. `Repo._verify_plan` refuses such a plan with "re-run
the plan". The per-object kinds are required per action instead; see
:data:`REQUIRED_ACTION_PRECONDITIONS`."""

REQUIRED_ACTION_PRECONDITIONS: dict[str, dict[str, tuple[frozenset[str], ...]]] = {
    "new": {
        "fork": (frozenset({"ref_head", "ref_absent"}),),
        "reuse": (frozenset({"ref_head"}),),
        "adopt": (frozenset({"ref_head"}),),
    },
    "restore": {"fork": (frozenset({"ref_head", "ref_absent"}),)},
    "promote": {
        "fast-forward": (
            frozenset({"base_state"}),
            frozenset({"ref_head", "pin_state"}),
        ),
        "merge": (frozenset({"base_state"}), frozenset({"ref_head", "pin_state"})),
    },
}
"""Per command and action verb, the precondition kinds that must name the
action's object: each inner set is a group of alternatives, one of which
must be present (a fork carries `ref_head` when its branch existed at plan
time and `ref_absent` when it did not)."""


@dataclass(frozen=True)
class Precondition:
    """One thing that must still hold when a plan is applied.

    The drift contract in data: a plan records what it saw (a manifest hash,
    a branch head, a history digest) and apply refuses with
    :class:`~tether.errors.StalePlanError` if the world says otherwise. Kept
    out of `context` so the checks are typed, listed, and run in one place
    rather than re-implemented per command.

    Attributes:
        kind: One of :data:`PRECONDITION_KINDS`.
        expected: The value seen at plan time (a hash, a state, a commit id).
        key: The object key, for per-object checks.
        params: What the check needs to look again (`locator`, `backend`,
            `ref`, `rev`, `pin`, `bookmark`).
        detail: The refusal message; may use `{observed}`.
    """

    kind: str
    expected: Any = None
    key: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "expected": self.expected,
            "key": self.key,
            "params": dict(self.params),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Precondition:
        kind = str(data["kind"])
        if kind not in PRECONDITION_KINDS:
            raise ConfigError(f"unknown plan precondition {kind!r}")
        return cls(
            kind=kind,
            expected=data.get("expected"),
            key=data.get("key"),
            params=dict(data.get("params") or {}),
            detail=str(data.get("detail", "")),
        )


@dataclass
class Action:
    """One store-writing step in a `Plan`."""

    op: str
    """`pin`, `record`, `fork`, `defer-fork`, `reuse`, `adopt`, `trunk`, `refuse`,
    `unpin`, `keep-pin`, `delete-branch`, `keep-branch`, `forget-working-ref`,
    `delete-listing`, `fast-forward`, `merge`, `add`, `update`, `remove`,
    `upsert`, `repin`, `refork`, or `vcs-commit`."""
    key: str = ""
    """Object key the action concerns (empty for repository-level steps)."""
    kind: str = ""
    """Backend kind."""
    target: str = ""
    """Native ref / branch / file the action creates, deletes, or points at."""
    detail: str = ""
    """Human-readable explanation."""
    params: dict[str, Any] = field(default_factory=dict)
    """Machine-readable inputs (state, pin id, source, ...) used by apply."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "key": self.key,
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        return cls(
            op=str(data["op"]),
            key=str(data.get("key", "")),
            kind=str(data.get("kind", "")),
            target=str(data.get("target", "")),
            detail=str(data.get("detail", "")),
            params=dict(data.get("params") or {}),
        )


@dataclass
class Plan:
    """The actions a command would perform, plus the inputs they depend on."""

    command: str
    """`commit`, `new`, `gc`, `promote`, `import`, `publish`, or `repair`."""
    actions: list[Action] = field(default_factory=list)
    """Store-writing steps, in execution order."""
    context: dict[str, Any] = field(default_factory=dict)
    """Inputs the plan was computed from (states, manifest hash, message, ...);
    apply re-validates these before writing."""
    notes: list[str] = field(default_factory=list)
    """Non-actions worth showing (objects skipped and why)."""
    preconditions: list[Precondition] = field(default_factory=list)
    """What must still hold at apply time; see :class:`Precondition`."""
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    saved_digest: str | None = field(default=None, compare=False, repr=False)
    """The `digest` a loaded plan was saved with (`None`: made in this
    process); `Repo._verify_plan` refuses a plan whose content no longer
    matches it."""

    NON_WRITES = frozenset(
        {
            "trunk",
            "keep-branch",
            "keep-pin",
            "keep-store",
            "defer-fork",
            "refuse",
            "reuse",
            "adopt",
            "hold",
            "share",
        }
    )
    """Actions that write nothing to an external system when applied."""

    @property
    def writes(self) -> list[Action]:
        """Actions that touch an external system or the working tree."""
        return [a for a in self.actions if a.op not in self.NON_WRITES]

    @property
    def is_empty(self) -> bool:
        return not self.writes

    def require(
        self,
        kind: str,
        expected: Any = None,
        *,
        key: str | None = None,
        detail: str = "",
        **params: Any,
    ) -> None:
        """Append a precondition (see :class:`Precondition`)."""
        if kind not in PRECONDITION_KINDS:
            raise ValueError(f"unknown precondition kind {kind!r}")
        self.preconditions.append(
            Precondition(kind, expected, key=key, params=params, detail=detail)
        )

    def _content(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "command": self.command,
            "created_at": self.created_at,
            "context": self.context,
            "notes": list(self.notes),
            "preconditions": [p.to_dict() for p in self.preconditions],
            "actions": [a.to_dict() for a in self.actions],
        }

    def digest(self) -> str:
        """SHA-256 of everything else in the saved plan, as JSON reads it back
        (keys sorted, values through `str` where JSON has no type).

        The preconditions vouch for the world the actions and context were
        computed from; the digest binds the three together, so a saved plan
        edited in one place -- an action dropped, a `target_state` or
        `bookmark_commit` changed -- is refused at apply rather than applied
        under checks that describe another plan. It detects edits, it does
        not authenticate: whoever can write the file can recompute it.
        """
        normalized = json.loads(json.dumps(self._content(), default=str))
        text = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self._content(), "digest": self.digest()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        fmt = int(data.get("format", PLAN_FORMAT))
        if fmt in _UNBOUND_FORMATS:
            raise StalePlanError(f"{_UNBOUND_FORMATS[fmt]}; re-run the plan")
        if fmt != PLAN_FORMAT:
            raise ConfigError(f"unsupported plan format {fmt!r}")
        return cls(
            command=str(data["command"]),
            actions=[Action.from_dict(a) for a in data.get("actions", [])],
            context=dict(data.get("context") or {}),
            notes=[str(n) for n in data.get("notes", [])],
            preconditions=[
                Precondition.from_dict(p) for p in data.get("preconditions", [])
            ],
            created_at=str(data.get("created_at", "")),
            # A saved plan with no digest was edited as surely as one with
            # the wrong digest.
            saved_digest=str(data.get("digest") or ""),
        )

    def edited(self) -> bool:
        """Whether this plan was loaded and its content no longer matches the
        digest it was saved with (see :meth:`digest`)."""
        return self.saved_digest is not None and self.saved_digest != self.digest()

    @classmethod
    def from_json(cls, text: str) -> Plan:
        try:
            return cls.from_dict(json.loads(text))
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"invalid plan: {exc}") from exc

    def missing_preconditions(self) -> list[str]:
        """What this plan lacks of :data:`REQUIRED_PRECONDITIONS` and
        :data:`REQUIRED_ACTION_PRECONDITIONS`, as `kind` or `kind for key`
        entries; empty when it carries everything its command requires."""
        have = {p.kind for p in self.preconditions}
        missing = sorted(REQUIRED_PRECONDITIONS.get(self.command, frozenset()) - have)
        per_key: dict[str | None, set[str]] = {}
        for p in self.preconditions:
            per_key.setdefault(p.key, set()).add(p.kind)
        for a in self.actions:
            groups = REQUIRED_ACTION_PRECONDITIONS.get(self.command, {}).get(a.op, ())
            for group in groups:
                if not group & per_key.get(a.key, set()):
                    missing.append(f"{'/'.join(sorted(group))} for {a.key!r}")
        return missing

    def render(self) -> list[str]:
        """Human-readable lines, one per action, then notes."""
        lines: list[str] = []
        if not self.actions:
            lines.append(f"  {self.command}: nothing to do")
        width = max((len(a.key) for a in self.actions), default=0)
        for a in self.actions:
            label = f"{a.key:<{width}}  " if width else ""
            kind = f"[{a.kind}] " if a.kind else ""
            lines.append(f"  {a.op:>18}  {label}{kind}{a.target}  {a.detail}".rstrip())
        for note in self.notes:
            lines.append(f"  {'':>18}  {note}")
        return lines
