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
