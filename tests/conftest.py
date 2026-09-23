"""Shared pytest fixtures.

Creating real git and jj repositories requires actual VCS binaries and syscalls
that a sandbox may block; run the suite with a real environment (CI runs it in a
clean container).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pytest_postgresql import factories

# Where `jj` lives when it is not on PATH: an optional TETHER_TEST_BIN dir, else
# whatever `shutil.which` finds. Tests skip when neither yields a `jj`.
_BIN = os.environ.get("TETHER_TEST_BIN")


# --------------------------------------------------------------------------- #
# Ephemeral Postgres (publish / import tests)
# --------------------------------------------------------------------------- #
def _find_pg_ctl() -> str | None:
    """Locate ``pg_ctl`` on PATH or in the usual install dirs (Homebrew, Debian).

    ``None`` keeps collection working when Postgres is absent; tests that need
    it skip through ``pg_dsn`` instead of failing at import.
    """
    found = shutil.which("pg_ctl")
    if found:
        return found
    candidates = [
        "/opt/homebrew/opt/postgresql@17/bin/pg_ctl",
        "/opt/homebrew/opt/postgresql@16/bin/pg_ctl",
        "/usr/local/opt/postgresql@16/bin/pg_ctl",
        "/usr/lib/postgresql/17/bin/pg_ctl",
        "/usr/lib/postgresql/16/bin/pg_ctl",
        "/usr/lib/postgresql/15/bin/pg_ctl",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


_PG_CTL = _find_pg_ctl()
# One cluster per session; `postgresql` hands each test a fresh database.
postgresql_proc = (
    factories.postgresql_proc(executable=_PG_CTL)
    if _PG_CTL
    else factories.postgresql_proc()
)
postgresql = factories.postgresql("postgresql_proc")


@pytest.fixture
def pg_dsn(request: pytest.FixtureRequest) -> str:
    """libpq DSN of a fresh database on the session's ephemeral cluster.

    Skips when no Postgres binaries are installed.
    """
    if _PG_CTL is None:
        pytest.skip("PostgreSQL (pg_ctl) not installed")
    conn = request.getfixturevalue("postgresql")
    info = conn.info
    auth = info.user + (f":{info.password}" if info.password else "")
    return f"postgresql://{auth}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture(scope="session")
def _jj_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cfg = tmp_path_factory.mktemp("jjcfg") / "config.toml"
    cfg.write_text(
        '[user]\nname = "tether tests"\nemail = "tests@tether.dev"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, _jj_config: Path) -> None:
    if _BIN and os.path.isdir(_BIN) and shutil.which("jj") is None:
        monkeypatch.setenv("PATH", _BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("JJ_CONFIG", str(_jj_config))
    # git needs an identity to commit, and a CI runner has none configured.
    # Every repository a test makes -- a created store, a clone, a subprocess
    # -- inherits this one, the way jj gets its identity from JJ_CONFIG above.
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "tether tests")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "tests@tether.dev")
    monkeypatch.delenv("TETHER_REV", raising=False)


@pytest.fixture
def hostile_vcs_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """A user whose jj and git settings would break every id tether parses if
    they reached its calls: colour forced on, the keywords tether's
    templates read aliased (`commit_id` and `change_id` swapped, bookmark
    names suffixed, `conflict` always false), `all()` aliased to the working
    copy and `conflicts()`, `working_copies()` and `root()` to nothing, new
    files never tracked and capped at 1 KiB, signatures shown, paths quoted,
    untracked files hidden from `git status`. jj itself still works under
    it. Tests that take this fixture must behave exactly as under the plain
    config; their own jj templates call keywords as methods."""
    cfg = tmp_path_factory.mktemp("hostile")
    (cfg / "jj.toml").write_text(
        '[user]\nname = "tether tests"\nemail = "tests@tether.dev"\n'
        '[ui]\ncolor = "always"\n'
        "[template-aliases]\n"
        "commit_id = 'self.change_id()'\n"
        "change_id = 'self.commit_id()'\n"
        "name = 'self.name() ++ \"_aliased\"'\n"
        "conflict = 'false'\n"
        '[revset-aliases]\n"all()" = "@"\n"conflicts()" = "none()"\n'
        '"working_copies()" = "none()"\n"root()" = "none()"\n'
        '[snapshot]\nauto-track = "none()"\nmax-new-file-size = "1KiB"\n',
        encoding="utf-8",
    )
    (cfg / "gitconfig").write_text(
        "[color]\n\tui = always\n[log]\n\tshowSignature = true\n"
        "[core]\n\tquotePath = true\n[status]\n\tshowUntrackedFiles = no\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JJ_CONFIG", str(cfg / "jj.toml"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg / "gitconfig"))
    return cfg


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tether"], cwd=path, check=True)


def _jj_init(path: Path) -> None:
    subprocess.run(["jj", "git", "init"], cwd=path, check=True, capture_output=True)


@pytest.fixture(params=["git", "jj"])
def vcs_root(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    if shutil.which(request.param) is None:
        pytest.skip(f"{request.param} not on PATH")
    if request.param == "git":
        _git_init(tmp_path)
    else:
        _jj_init(tmp_path)
    return tmp_path
