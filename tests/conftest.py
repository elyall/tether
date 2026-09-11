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

_BIN = "/Users/elyall/Documents/Code/.bin"


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
    if os.path.isdir(_BIN):
        monkeypatch.setenv("PATH", _BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("JJ_CONFIG", str(_jj_config))
    monkeypatch.delenv("TETHER_REV", raising=False)


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
