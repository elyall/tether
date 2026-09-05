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

_BIN = "/Users/elyall/Documents/Code/.bin"


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
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
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
