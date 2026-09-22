"""The registry commands (`export`, `publish`, `import`), registered onto the
main Typer app by :func:`register`.

Experimental: the schema may change before 0.2, and every command prints the
note. Kept out of :mod:`tether.cli` so that module stays the core loop; the
command names are stable and do not change if this layer graduates.
"""

from __future__ import annotations

from pathlib import Path

import typer

from tether.cli import (
    _emit,
    _experimental_note,
    _fail,
    _load_plan,
    _repo,
    _save_plan,
    _show_plan,
)
from tether.errors import ConfigError, TetherError
from tether.plan import Action, Plan

_REGISTRY_NOTE = (
    "export/publish/import are experimental (tether.experimental.registry): the "
    "schema may change before 0.2"
)
_EXPORT_FORMATS = ("sqlite", "parquet", "csv", "jsonl")


def _echo_counts(counts: dict[str, int]) -> None:
    width = max((len(k) for k in counts), default=0)
    for name, n in counts.items():
        typer.echo(f"  {name:<{width}}  {n:>7} row(s)")


def register(app: typer.Typer) -> None:
    """Attach `export`, `publish`, and `import` to `app`."""

    @app.command()
    def export(
        path: Path = typer.Argument(
            ..., help="Output: a .sqlite file, or a directory for parquet/csv/jsonl."
        ),
        rev: list[str] = typer.Option(
            [], "--rev", "-r", help="Export only these revisions (repeatable)."
        ),
        all_history: bool = typer.Option(
            True,
            "--all-history/--no-all-history",
            help="Export every reachable commit (default when no --rev is given).",
        ),
        fmt: str = typer.Option(
            "sqlite", "--format", help="sqlite (default), parquet, csv, or jsonl."
        ),
        listings: bool = typer.Option(
            False,
            "--listings",
            help="Include per-file listings (listings, listing_entries).",
        ),
        workspace: bool = typer.Option(
            False,
            "--workspace",
            help="Include this checkout's working refs and snapshot.",
        ),
        append: bool = typer.Option(
            False,
            "--append",
            help="sqlite: upsert into an existing file instead of replacing it.",
        ),
        json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """[experimental] Write tether history as relational tables for registries.

        Tables: commits, commit_parents, refs, objects, object_states, plus
        listings / listing_entries (--listings) and workspace (--workspace); see
        the Registries guide for the schema. Query the SQLite file directly or
        `ATTACH` it in DuckDB; the parquet/csv/jsonl directories get one file per
        table and a schema.json.
        """
        if fmt not in _EXPORT_FORMATS:
            _fail(TetherError(f"--format must be one of {', '.join(_EXPORT_FORMATS)}"))
        if not json_out:  # machine consumers get JSON alone
            _experimental_note(_REGISTRY_NOTE)
        if not rev and not all_history:
            rev = ["@"] if _repo().vcs.kind == "jj" else ["HEAD"]
        repo = _repo()
        try:
            bundle = repo.export(rev or None, listings=listings, workspace=workspace)
            if fmt == "sqlite":
                written = [bundle.to_sqlite(path, append=append)]
            else:
                written = bundle.to_dir(path, fmt)
        except TetherError as exc:
            _fail(exc)
        counts = bundle.row_counts()
        if json_out:
            _emit(
                {"path": str(path), "format": fmt, "head": bundle.head, "rows": counts},
                as_json=True,
            )
            return
        typer.echo(f"exported {len(written)} file(s) to {path} ({fmt})")
        _echo_counts(counts)

    @app.command()
    def publish(
        to: str | None = typer.Option(
            None,
            "--to",
            help="Postgres DSN (postgresql://...). Defaults to $TETHER_PUBLISH_DSN.",
            envvar="TETHER_PUBLISH_DSN",
            show_envvar=True,
        ),
        schema: str = typer.Option("tether", "--schema", help="Target schema name."),
        rev: list[str] = typer.Option(
            [], "--rev", "-r", help="Publish only these revisions (repeatable)."
        ),
        listings: bool = typer.Option(
            False,
            "--listings",
            help="Include per-file listings (listings, listing_entries).",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what would be written per table; write nothing.",
        ),
        json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """[experimental] Upsert the export tables into a Postgres schema.

        Idempotent and incremental: commits already present are skipped, `refs`
        and `tether_meta` are refreshed. Run it after each `tether commit` (or from
        CI) so a registry can join `objects.uri` / `objects.commit_id` against its
        own tables. The DSN never comes from tether.toml.
        """
        if not to:
            _fail(
                TetherError("a Postgres DSN is required: --to or $TETHER_PUBLISH_DSN")
            )
        if not json_out:  # machine consumers get JSON alone
            _experimental_note(_REGISTRY_NOTE)
        repo = _repo()
        try:
            bundle = repo.export(rev or None, listings=listings)
            report = bundle.to_postgres(to, schema=schema, dry_run=dry_run)
        except TetherError as exc:
            _fail(exc)
        if dry_run:
            plan = Plan(
                command="publish", context={"schema": schema, "head": bundle.head}
            )
            for name, n in report.upserted.items():
                if n:
                    plan.actions.append(
                        Action(
                            "upsert", target=f"{schema}.{name}", detail=f"{n} row(s)"
                        )
                    )
            if report.skipped_commits:
                plan.notes.append(
                    f"{report.skipped_commits} commit(s) already published"
                )
            _show_plan(plan, as_json=json_out)
            return
        if json_out:
            _emit(
                {
                    "schema": schema,
                    "head": bundle.head,
                    "upserted": report.upserted,
                    "skipped_commits": report.skipped_commits,
                },
                as_json=True,
            )
            return
        total = sum(report.upserted.values())
        typer.echo(
            f"published {total} row(s) to schema {schema!r} "
            f"({report.skipped_commits} commit(s) already present)"
        )
        _echo_counts(report.upserted)

    @app.command(name="import")
    def import_(
        source: str = typer.Argument(
            ...,
            help="Postgres DSN, SQLite file, .csv, or .jsonl with the canonical "
            "columns.",
        ),
        table: str | None = typer.Option(
            None, "--table", help="SQL sources: read every row of this table."
        ),
        query: str | None = typer.Option(
            None,
            "--query",
            help="SQL sources: run this query (must yield key, kind, and locator "
            "columns). "
            "Defaults to [import] query in .tether/secrets.toml.",
        ),
        sync: bool = typer.Option(
            False,
            "--sync",
            help="Also remove registered objects the source does not list.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the add/update/remove plan; write nothing."
        ),
        plan_out: Path | None = typer.Option(
            None, "--plan", help="Write the plan to FILE (implies --dry-run)."
        ),
        from_plan: Path | None = typer.Option(
            None,
            "--from-plan",
            help="Apply a plan saved with --plan instead of re-reading.",
        ),
        json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """[experimental] Register or sync objects from a registry query.

        Rows carry `key`, `kind`, and any of `uri`, `locator_json`, `policy_file`,
        `policy_pin`, `at`; write the mapping from your registry's
        columns in SQL. Import changes what is tracked (manifests only) -- run
        `tether commit` afterwards to record states. `--sync` removes objects the
        source no longer lists.
        """
        from tether.experimental.registry import (
            is_sql_source,
            read_source,
            specs_from_rows,
        )

        if not json_out:  # machine consumers get JSON alone
            _experimental_note(_REGISTRY_NOTE)
        repo = _repo()
        try:
            if from_plan is not None:
                plan = _load_plan(from_plan, "import")
                report = repo.apply_import(plan)
            else:
                if table is None and query is None:
                    query = repo.secrets.import_query
                    if (
                        query is None
                        and repo.config.committed_import_query
                        and is_sql_source(source)
                    ):
                        raise ConfigError(
                            "tether.toml sets [import] query; a committed file "
                            "arrives with every clone and must not choose SQL "
                            "that runs with your DSN. Move it to "
                            ".tether/secrets.toml under [import], or pass --query"
                        )
                rows = read_source(source, table=table, query=query)
                specs, notes = specs_from_rows(rows, repo.config.defaults)
                plan = repo.plan_import(specs, sync=sync, notes=notes)
                if dry_run or plan_out is not None:
                    _save_plan(plan, plan_out)
                    _show_plan(plan, as_json=json_out)
                    return
                report = repo.apply_import(plan)
        except (TetherError, OSError, ValueError) as exc:
            _fail(exc)
        if json_out:
            _emit(
                {
                    "added": report.added,
                    "updated": report.updated,
                    "removed": report.removed,
                    "unchanged": report.unchanged,
                },
                as_json=True,
            )
            return
        typer.echo(
            f"added {len(report.added)}, updated {len(report.updated)}, "
            f"removed {len(report.removed)}, unchanged {len(report.unchanged)}"
        )
        for key in report.added:
            typer.echo(f"  added   {key}")
        for key in report.updated:
            typer.echo(f"  updated {key}")
        for key in report.removed:
            typer.secho(f"  removed {key}", fg=typer.colors.YELLOW)
        if report.added or report.updated:
            typer.echo("run `tether commit` to record their states")
