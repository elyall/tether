# Road to 0.1.0

What has to happen between the 0.1.0 betas and the first release that is
not one. Four lists: what still blocks a correct 0.1.0, what has only ever
run against a stand-in and needs a real resource under it, which tests are
missing, and what is experimental today and has to graduate or be cut.
Everything here is a decision or a test, not a feature.

One definition runs through all of it. **Stable** means the full lifecycle
-- register, commit, fork, write, commit, promote, gc -- is *exercised in
CI* against the real system. **Experimental** means it has run against a
fake, a local stand-in, or not at all. By that definition `file` and
`icechunk` are stable on local storage, which is what CI runs; their S3, GCS
and Azure paths are **not yet cloud-tested** and are labelled so in the
README and the backends guide until section 2's runs happen. `tether
backends` prints the label per kind; `add`, `--create`, and
`gc --delete-stores` print the note.

## 1. Correctness blockers

The beta-exit work (0.1.0b4) closed the review's
findings: git hardening and the trust boundary for a cloned dataset, a `gc`
that releases only what this clone created and counts every live checkout,
jj and git isolated from the user's configuration, plans bound to their
checkout and bookmark, conditional ref moves, per-thread locks, and the
backend and CLI fixes. What remains is coverage, not known bugs:

| Blocker | What is missing | What settles it |
| --- | --- | --- |
| **Real-service runs** for `neon`, `dolt`, `iceberg`, `ducklake` | Every one of them has run only against a fake or a local stand-in. Rate limits, eventual consistency, permission models and the cost of a Neon branch per commit are unobserved. | The rows in section 2, once each, with the result recorded; a CI row where a credential or a container can live there. |
| **Cloud runs** for `file` and `icechunk` on S3, GCS and Azure | The object-store code paths -- obstore options such as `allow_http`, versioned objects, `s3_storage` with per-object credentials, `delete_store` on a prefix -- have run against in-memory stores and S3-compatible fakes only. | The S3, credentials, GCS and Azure rows in section 2. Until then the labels say "not yet cloud-tested". |
| **Windows** | Writing commands are refused where `fcntl` is missing; read-only ones work. Nothing has run on Windows at all. | A decision: implement the checkout and repository locks on `msvcrt`, or ship 0.1.0 with writes refused on Windows and say so in the README. Either way a Windows CI row (section 3). |
| **jj version range** | The minimum is jj 0.43.0 (the version tether's tests run against locally); CI installs 0.45.1 only. Nothing between or beyond has run. | The version-matrix row in section 3. |
| **jj template aliases** | tether's revsets use operator forms, so a user's revset aliases cannot change what it walks. Its `jj log -T` templates are not defended the same way: a `[template-aliases]` entry that shadows a keyword tether uses could change what it reads. | Pass a controlled config layer for templates as for revsets, or check the output shape; the hostile-config fixture (section 3) gets a template alias. |
| **git `promote`/`merge` under a detached `HEAD`** | With the default `ref = HEAD` the git backend moves the checked-out branch, and refuses a detached `HEAD`, which a colocated jj checkout always has. Documented; not fixed. | Decide whether to merge without a checkout (`merge-tree --write-tree`, `commit-tree`, a conditional `update-ref`) before 0.1.0, or keep the documented refusal. |
| **A teardown command** | Removing tether from a repository is a manual recipe in the Troubleshooting page: list and delete every `tether.<id>.*` ref per store. | A command, or the recipe stays and is exercised once by hand against every stable backend. |

## 2. Test against actual resources

Each line names the resource, what to run, and what would move as a result.
The engine tests and conformance suites cover the logic; these cover the
service. Run them once by hand before the release and, where a credential
can live in CI, keep them running.

| Resource | What to run | What it decides |
| --- | --- | --- |
| **S3 bucket** (any region; versioning on for one prefix) | `file` prefix and versioned-object objects: `add`, `commit`, `status --snapshot`, `verify`, `open --rev`, drift after an overwrite, `allow_http` and the other allowlisted `storage_options`. `icechunk` repository at `s3://`: the story of section 2 of the worked examples (fork, write, promote, gc). `add --create` / `gc --delete-stores` on an `s3://` prefix -- the `delete_store` arm has only run against an obstore in-memory store. | `file`'s Addressable tier for versioned objects is real; Icechunk-on-S3 is real; both drop the "not yet cloud-tested" label; store-lifecycle graduation criterion 1. |
| **AWS credentials via `secrets.toml`** (a `profile`, a `role_arn`, literal keys, an `endpoint_url` to a SeaweedFS) | Two Icechunk objects in two accounts pinned by one `commit` (the configuration guide's "two identities" section). `tether open` and `verify` with `region` overrides. A `Repo` held open past the assumed role's expiry (the credentials refresh five minutes before it). | The per-object credential layer is real (it is core today; the reviews asked twice whether it should be). |
| **GCS and Azure containers** | `file` objects and prefixes through obstore (`gs://`, `az://`); a versioned object on each. | Whether the `file` backend's object-store claims hold beyond S3, or the docs narrow to S3. |
| **Neon project** (Free for the default unprotected pins, which still cap a project at 10 branches; a paid plan, which allows a few protected branches, for `protected_pins = true`) | The `neon` lifecycle against the control plane: `add`, `commit` (a branch pin, protected when asked), `new` + writable open (branch), `promote` refusal path, `gc --prune-bookmarks` keeping a `BRANCH_IS_STORAGE` branch, `--force-prune` deleting one, `verify` with a suspended compute, a 423 answer during a pin. The lineage / xid caveat in the caveats guide, observed. | `neon` stays experimental at 0.1.0 (below); this run decides what the note says. |
| **Iceberg catalog** (a REST catalog -- Lakekeeper or Polaris in a container -- and one object-store warehouse) | The `iceberg` lifecycle: snapshot pins, branch forks, `promote` fast-forward, `log`, `add --pick`, a table with no snapshot yet, retention expiring a recorded state. pyiceberg 0.11 or newer. | `iceberg` graduates; the `RETENTION_BOUND` claim is observed rather than asserted. |
| **Dolt server** (container, MySQL protocol) | The `dolt` lifecycle including a conflicting `merge` (it must surface as `MergeConflict`), `HASHOF` pins, and a server whose `[uris]` entry is missing (no password may be sent). | `dolt` graduates. |
| **DuckLake catalog** (DuckDB with a Postgres or SQLite catalog and an object-store `data_path`) | Addressable reads by snapshot id; `log`; retention; a relative `metadata` path registered from another directory. | `ducklake` graduates. |
| **Postgres** (already in CI through `pytest-postgresql`) | Nothing new: the registry publish/import path runs against it. Run once against a managed Postgres (RDS, Neon) to catch permission and extension differences. | Registry graduation, together with a schema decision (below). |
| **A hosted git remote and a hosted jj remote** (GitHub; a jj-capable forge or a bare repository over SSH) | Two clones of one dataset: the two-clone scenario in `tests/test_store_lifecycle.py` against a real remote, including a `forget-workspace` on one side; `gc` in each clone before and after fetching (`keep-pin` until fetched, `--release-foreign` after); a bookmark pushed from one side and dropped on the other. `tether abandon` under `git` with a remote-tracking branch. A `git` object whose `remote` is configured, pinned from a clone. | Store-lifecycle graduation criterion 2; whether "gc only knows what this clone has fetched" needs more than documentation. |
| **A large local tree** (10^6 files) and **a large prefix** | `status` first and second fingerprint; `commit` with a listing; `diff --content`. Numbers into the performance guide. | The performance guide's claims are measured, not estimated. |

## 3. Tests to add

Rows that need no external resource, only time. Each becomes a job or a
fixture in `tests/`.

| Test | What it runs | What it decides |
| --- | --- | --- |
| **jj version matrix with a hostile user config** | The core loop, `gc`, `drop` and `undo` under jj 0.43.0 (the minimum), the CI pin (0.45.1) and the newest release, each with the hostile-config fixture from `tests/test_vcs_isolation.py` (colour forced on, `all()` aliased, auto-tracking off, `log.showSignature`) plus a `[template-aliases]` entry shadowing a keyword tether's templates use. | Whether 0.43.0 stays the minimum, and whether templates need the defence revsets have. |
| **A hostile clone, end to end** | One fixture repository whose manifests try everything the trust boundary refuses: a relative or in-checkout git `path`, a URL `remote`, a committed `[import] query`, `git_path`, an endpoint in `storage_options`, keys with `..` and `\`, a dataset id reused from another dataset naming the same store. Every command runs against it and must refuse without contacting anything it should not. | The security-model section of the configuration guide is a test, not a promise. |
| **Multi-checkout interleavings** | Two jj workspaces (and two git worktrees) of one dataset stepping through `commit` vs `gc`, `new --shared` vs the first writable `open`, `drop` vs `new` on the dropped bookmark, `restore` vs a `--shared` peer's write, `promote` vs a commit on the trunk, in every order the locks allow. | The repository lock and the `expected`-head moves hold under interleaving, not only in the two races the review reproduced. |
| **Windows** | The read-only commands (`status`, `verify`, `diff`, `log`, `ops`, `gc --dry-run`) on a Windows runner, and the refusal message for a writing one; if the lock is implemented, the full suite. | The Windows decision in section 1. |
| **Every backend through the full conformance suite** | The `file` backend with its real capabilities (`DIFF`, `CREATE`, `ADDRESSABLE` for a versioned object), not the `FINGERPRINT`-only override the in-repo harnesses use today; `delete_store` refusing a non-empty store for every `CREATE` backend. | The suite's claims cover what ships. |

## 4. Experimental today; graduate or cut before 0.1.0

| Feature | Where | Graduates when | The call for 0.1.0 |
| --- | --- | --- | --- |
| **`iceberg`** | `tether.experimental.backends.iceberg` | The 0.1.0b4 fixes (empty tables, pyiceberg 0.11) are in; what is left is the catalog row in section 2 as a CI job (a REST catalog in a container) and the conformance suite passing against it. Then move the module to `tether/backends/`, set `MATURITY = "stable"`, update the backends guide and README. | **Next to graduate**, after its CI row. |
| **`ducklake`** | `tether.experimental.backends.ducklake` | The 0.1.0b4 fix (absolute `metadata` path) is in; what is left is the section 2 row as a CI job (DuckDB with the Postgres catalog `pytest-postgresql` already provides). | **Next to graduate**, after its CI row. |
| **`dolt`** | `tether.experimental.backends.dolt` | The 0.1.0b4 and b7 fixes (per-server credentials, merges inside a transaction) are in; the section 2 row against `dolt sql-server` in a container, as a CI job, and the conformance suite against it. | Graduates **after its fixes are observed live**; otherwise ships experimental. |
| **`neon`** | `tether.experimental.backends.neon` | A live run of the section 2 row, and a cost story users accept: one branch per pinning commit, unprotected by default, protected pins on paid plans only. | **Stays experimental** at 0.1.0, with the note. |
| **Registry** (`export`, `publish`, `import`; `tether.experimental.registry`) | `tether.experimental.registry` | The export schema is declared frozen (a `schema_version` in the bundle and a documented compatibility promise), one round trip has run against a managed Postgres, and `import --sync` has been used on a real dataset. Move to `tether.registry`. | **Stays experimental** past 0.1.0 with a stated horizon. It does not block the release: nothing else depends on it. |
| **Store lifecycle** (`add --create`, `Repo.create`, `gc --delete-stores`, `--store`; `tether.experimental.lifecycle`) | `tether.experimental.lifecycle`; `Capability.CREATE` in `tether.backends.base` | The five criteria in the module docstring: S3 `delete_store` against a real bucket; the two-clone scenario against a real remote; one release cycle with no data-loss report; a second Forkable backend with `CREATE`; a decision on the touched index. Move the module to `tether/repo/_lifecycle.py`, drop the notes, re-home the CHANGELOG entry. | **Stays experimental** at 0.1.0. `Capability.CREATE` stays declared (it is a per-backend fact with conformance coverage). |
| **Per-URI and per-object credentials** (`secrets.toml` `[uris.*]` / `[objects.*]`; `ObjectBackend.configure_secrets` / `secrets_for`) | core (`tether.backends.base`, `Repo.backend_for`); the reviews asked twice whether it belongs in experimental | The two-identity run in section 2 has happened against real accounts (a `profile`, a `role_arn`, literal keys, an `endpoint_url`) for Icechunk and `file`, and no second resolution order was needed. Then it is simply core, and the configuration guide's "advanced" section is the contract. | Moves to `tether.experimental.credentials` behind `configure_secrets` if the run finds a second order is needed; the kind-level `[backends.<kind>]` secrets stay core either way. Nothing users write in `secrets.toml` changes. |
| **The alpha upgrade path** (`tether.upgrade`) | `tether.upgrade` | Does not graduate: it is *removed* at 0.1.0 as announced. The removal checklist is below. | -- |
| **`tether abandon`** | core (`Repo.abandon`) | Decided: it stays as the surgical form -- commits off a bookmark you are keeping, `--gc` to release their pins in one action -- while `drop` takes the whole line. The remaining question is only whether it behaves under `git` with remote-tracking branches (the two-clone and remote tests above). | Kept; documented next to `drop` as the pair they are. |

### Removing the upgrade path at 0.1.0

Before removal: publish the last beta, confirm `tether upgrade` from every
alpha format against it once more, and write the "install
`tether-vcs==<last beta>`, upgrade, reinstall" message into the open-time
error (`LAST_BETA_WITH_UPGRADE` names it). Then remove, in one revision:

- the `tether.upgrade` package, the `upgrade` CLI command, and the thin
  `Repo.plan_upgrade` / `apply_upgrade` / `upgrade` delegates;
- `Repo.find(allow_outdated=)` and the `allow_outdated` constructor argument,
  which exist only so `upgrade` can open an older dataset;
- `ObjectBackend.rename_pin` and `rename_working_ref` (and the Neon
  override), which only the ref-renaming migration calls;
- `working_ref_workspace` and the legacy per-workspace working-ref parsing
  (`gc --prune-bookmarks`'s "legacy branch" arm), the `UpgradeReport`
  re-export, and the Migrations section of `great-docs.yml`;
- the `upgrade` sections of the CLI guide and the configuration guide,
  replaced by the open-time message;
- freeze `CONFIG_VERSION` for 0.1.x.

## 5. Before tagging 0.1.0

- Every backend the README matrix lists is either stable or marked
  *(experimental)* in the row; the matrix, the backends guide, and `tether
  backends` agree, and "stable" means exercised in CI everywhere it appears.
  `file` and `icechunk` either have their cloud rows run or keep the "not
  yet cloud-tested" note.
- The blockers in section 1 are each closed or decided, and the tests in
  section 3 run in CI.
- `tether.upgrade` is gone by the checklist above; `CONFIG_VERSION` is
  frozen for 0.1.x; a dataset from the last beta opens without `upgrade`.
- The performance guide's numbers come from section 2's runs.
- The changelog's `0.1.0` entry lists what graduated, what stayed
  experimental, and what was removed, in those words.
