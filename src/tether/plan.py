"""Plans: what a store-writing command *would* do, as data.

Every command that writes to an external system (`commit` pins, `new` forks,
`gc` unpins / deletes branches) is split into a read-only *plan* step and an
*apply* step. A `Plan` lists the concrete `Action`s with the inputs they were
computed from, serializes to JSON (`tether commit --dry-run --json`), and can be
applied later (`tether commit --from-plan plan.json`); apply re-checks that the
world still matches the plan before touching anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tether.errors import ConfigError

PLAN_FORMAT = 1


@dataclass
class Action:
    """One store-writing step in a `Plan`."""

    op: str
    """`pin`, `record`, `fork`, `defer-fork`, `track`, `unpin`, `delete-branch`,
    `keep-branch`, `forget-working-ref`, `delete-listing`, `add`, `update`,
    `remove`, `upsert`, or `vcs-commit`."""
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
    """`commit`, `new`, or `gc`."""
    actions: list[Action] = field(default_factory=list)
    """Store-writing steps, in execution order."""
    context: dict[str, Any] = field(default_factory=dict)
    """Inputs the plan was computed from (states, manifest hash, message, ...);
    apply re-validates these before writing."""
    notes: list[str] = field(default_factory=list)
    """Non-actions worth showing (objects skipped and why)."""
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    NON_WRITES = frozenset({"track", "keep-branch", "defer-fork"})
    """Actions that write nothing to an external system when applied."""

    @property
    def writes(self) -> list[Action]:
        """Actions that touch an external system or the working tree."""
        return [a for a in self.actions if a.op not in self.NON_WRITES]

    @property
    def is_empty(self) -> bool:
        return not self.writes

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "command": self.command,
            "created_at": self.created_at,
            "context": self.context,
            "notes": list(self.notes),
            "actions": [a.to_dict() for a in self.actions],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        if int(data.get("format", PLAN_FORMAT)) != PLAN_FORMAT:
            raise ConfigError(f"unsupported plan format {data.get('format')!r}")
        return cls(
            command=str(data["command"]),
            actions=[Action.from_dict(a) for a in data.get("actions", [])],
            context=dict(data.get("context") or {}),
            notes=[str(n) for n in data.get("notes", [])],
            created_at=str(data.get("created_at", "")),
        )

    @classmethod
    def from_json(cls, text: str) -> Plan:
        try:
            return cls.from_dict(json.loads(text))
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"invalid plan: {exc}") from exc

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
