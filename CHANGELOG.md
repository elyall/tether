# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial project scaffold.
- Manifest model (`tether.toml`, per-object manifests, workspace state) with
  canonical hashing and content-addressed pin ids.
- VCS adapters for `jj` and `git` with stale-working-copy detection.
- Backend protocol with declared capability tiers, in-memory reference backend,
  and an importable, capability-parametrized conformance suite.
- Orchestration engine: fan-out snapshot, status, commit, new, open, verify, gc.
- Backends: `file`, `icechunk`, `neon`, `git`, `iceberg`.
- Typer CLI (`tether`).
- Backends: `delta` (Addressable, retention-bound), `lance` (Forkable: tags on
  `(branch, version)`, branches), `lakefs` (Forkable: tags, branches, dirty
  staging detection). Handles: `DeltaHandle`, `LanceHandle`, `LakeFSHandle`.
- `file` backend supports GCS (`gs://`) and Azure Blob (`az://`, `abfs://`)
  alongside S3 through obstore; `--file versioned` records GCS generations and
  Azure version ids like S3 version ids.
- `VcsAdapter.files_at` / `iter_history_files`: batched manifest reads through
  one `git cat-file --batch` process (also for jj repos).
- CLI: `tether add --repository/--prefix` for lakeFS locators.
- Backends: `ducklake` (Addressable, retention-bound: catalog-wide snapshot ids
  via DuckDB's `ducklake` extension, time travel through `SNAPSHOT_VERSION`) and
  `dolt` (Forkable over the MySQL protocol: `dolt_branches`/`dolt_tags` system
  tables, `DOLT_TAG`/`DOLT_BRANCH` procedures, dirty working-set detection).
  Handles: `DuckLakeHandle` (owns its attachment; context manager),
  `DoltHandle` (`db/ref` revision URL). CLI: `--host/--port/--table`.
- Content diffs: `Capability.DIFF`, `ObjectBackend.diff(locator, a, b, *,
  listings)` returning an `ObjectDiff` (unit, added/removed/modified counts,
  capped entries), and `tether diff --content [--limit N]` (`Repo.diff(...,
  content=True)`), fanned out across changed objects with per-object errors.
  Implemented natively for `file`, `git`, `icechunk`, `iceberg`, `delta`,
  `lance`, `lakefs`, `ducklake`, `dolt`, and `memory`; the conformance suite
  checks `diff` for every `DIFF` backend.
- Listings: `ObjectBackend.listing(locator, state)` lets a backend attach a
  detailed description of a state; the engine stores it content-addressed under
  `.tether/listings/<hash>.jsonl`, commits it with the manifests, reads it back
  (working tree or VCS) for diffs, and prunes unreferenced ones in `gc`. The
  `file` backend uses it for directory/prefix listings so Observed directories
  diff file by file.
- Documentation site built with [Great Docs](https://posit-dev.github.io/great-docs/)
  (`great-docs.yml`, `docs` dependency group pinning `great-docs` and
  `quarto-cli`, `.github/workflows/docs.yml` deploying to GitHub Pages). The
  API reference is generated from docstrings, the CLI reference from the Typer
  app (mirrored onto real Click objects by `tether._clickdoc`, since Typer
  vendors its own Click), and the user guide lives in `user_guide/`: getting
  started, concepts, pinning (worked example), branching and writing (worked
  example), CLI guide, configuration, backends, writing a backend.
- Docstrings: Google-style `Args`/`Returns`/`Raises` on every `Repo` method,
  attribute docstrings on result types, handles, `Capability`, `Tier`,
  `VerifyStatus`, and diff types; help text on every CLI option. The package
  root re-exports the result types, manifest helpers, and the `handles`,
  `backends`, `testing`, and `vcs` submodules.
- `tether.Policy` and `tether.Pin` are exported from the package root.
- `tether.toml`'s `[snapshot] auto` now sets the CLI default for
  `status`/`commit` snapshotting (`--no-snapshot` still wins) and
  `[new] auto_fork` makes `commit` re-fork working refs afterwards; both keys
  were previously parsed but unused.

### Changed

- The distribution is published as `tether-vcs` (PyPI prohibits the bare name
  `tether`); the importable package and the CLI remain `tether`. Install with
  `pip install tether-vcs[...]`.
- The `s3` extra now installs `obstore` instead of `boto3`; `objectstore`,
  `gcs`, and `azure` extras are aliases for the same dependency.
- `gc` and `verify --all-history` stream history through a single object
  reader, parse each distinct manifest once, and verify each distinct
  `(system, state, pin)` once, concurrently (~280x faster on a 200-commit repo).
- `verify` and the fork step of `new` fan out concurrently; the fan-out pool
  grew from 8 to 16 workers.
- Local directory fingerprints walk with `os.scandir` (~7x cheaper per file
  than `Path.rglob` + `stat`).
