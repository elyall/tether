"""Parts of tether that work but have not yet been run against the real thing.

Everything here is *experimental*: the backends under
:mod:`tether.experimental.backends` have been driven only through fakes or
local stand-ins (Neon's control-plane API mocked with respx, Dolt through an
in-memory client, Iceberg through a fake catalog), the registry
layer under :mod:`tether.experimental.registry` (`export`, `publish`, `import`)
is a young feature whose schema may still change, and the store lifecycle
under :mod:`tether.experimental.lifecycle` (`add --create`,
`gc --delete-stores`, `Repo.create`) is the one operation with no `repair`
path and has not yet run against the resources it was written for.

**What is stable, and what is not.** The stable surface is what users type
and import: kind names (`tether add --kind neon`), extras
(`tether-vcs[neon]`), CLI commands (`tether export` / `publish` / `import`),
the `tether.<Symbol>` re-exports (`ExportBundle`, `ImportSpec`, ...), and the
`Repo` methods that delegate here (`create`, `add(create=True)`,
`plan_gc(delete_stores=True)`). Module paths under `tether.experimental` are
**not** API and move without notice.

**Graduation is seamless.** `build_backend` resolves a kind through
`_BUILTIN_MODULES` (kind -> module path) and the `MATURITY` attribute drives
the label `tether backends` shows and the note `tether add` prints. To
graduate a backend: move its file to `tether/backends/`, set
`MATURITY = "stable"`, update the table in the docs. Nothing users type or
import changes, because none of it named the module path.
"""

from __future__ import annotations
