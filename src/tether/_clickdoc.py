"""Mirror the Typer CLI onto real Click objects for documentation tooling.

Typer vendors its own copy of Click, so the command tree
``typer.main.get_command(app)`` returns is not an instance of the ``click``
package's classes and tools that introspect Click CLIs (Great Docs) see a single
opaque command. This module rebuilds the tree with the installed ``click`` so
every subcommand, argument, and option is discoverable.

Documentation-only: importing it requires ``click`` (a Great Docs dependency),
and nothing in tether imports it at runtime.
"""

from __future__ import annotations

from typing import Any

import click
import typer

from tether.cli import app

# Typer's vendored param types report Python-ish names ("str", "int", ...).
_TYPES: dict[str, click.ParamType] = {
    "str": click.STRING,
    "text": click.STRING,
    "int": click.INT,
    "integer": click.INT,
    "float": click.FLOAT,
    "bool": click.BOOL,
    "boolean": click.BOOL,
    "path": click.Path(),
}


def _param_type(param: Any) -> click.ParamType:
    kind = str(getattr(param.type, "name", "text")).lower()
    choices = getattr(param.type, "choices", None)
    if choices:
        return click.Choice([str(c) for c in choices])
    return _TYPES.get(kind, click.STRING)


def _default(param: Any) -> Any:
    default = getattr(param, "default", None)
    return None if callable(default) else default


def _convert_param(param: Any) -> click.Parameter:
    if getattr(param, "param_type_name", "") == "argument":
        return click.Argument(
            [param.name],
            required=param.required,
            type=_param_type(param),
            default=_default(param),
            nargs=getattr(param, "nargs", 1),
        )
    decls = list(param.opts)
    if param.secondary_opts:
        decls = [f"{param.opts[0]}/{param.secondary_opts[0]}", *param.opts[1:]]
    return click.Option(
        decls,
        help=getattr(param, "help", None),
        required=param.required,
        type=None if param.is_flag else _param_type(param),
        is_flag=bool(param.is_flag),
        default=_default(param),
        multiple=bool(getattr(param, "multiple", False)),
        show_default=bool(getattr(param, "show_default", False)),
    )


def _convert_command(name: str, command: Any) -> click.Command:
    params = [_convert_param(p) for p in command.params if p.name != "help"]
    return click.Command(
        name=name,
        help=command.help,
        short_help=getattr(command, "short_help", None),
        epilog=getattr(command, "epilog", None),
        params=params,
    )


def build_click_group() -> click.Group:
    """Return a real ``click.Group`` mirroring the ``tether`` Typer app."""
    typer_group: Any = typer.main.get_command(app)  # a TyperGroup at runtime
    subcommands: dict[str, Any] = dict(getattr(typer_group, "commands", {}))
    return click.Group(
        name="tether",
        help=typer_group.help,
        commands={
            name: _convert_command(name, cmd)
            for name, cmd in sorted(subcommands.items())
        },
    )


click_app = build_click_group()
