"""The trust boundary: a cloned dataset's committed files are untrusted input.

`tether.toml` and the manifests arrive with every clone, so nothing in them may
choose an executable, an endpoint credentials are sent to, SQL to run, or
which environment variable holds a secret. Those live in the untracked
`.tether/secrets.toml` or the environment.
"""

from __future__ import annotations

import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any, cast

import pytest

from tether.errors import BackendError, ConfigError
from tether.manifest import (
    SECRETS_FILENAME,
    UNTRACKED_FILES,
    WORKSPACE_FILENAME,
    read_config,
    write_config,
)
from tether.oplog import OPS_FILENAME
from tether.repo import Repo


def _commit_config(repo: Repo, **vcs: object) -> None:
    config = read_config(repo.root)
    config.vcs.update(vcs)
    write_config(repo.root, config)


def test_committed_executable_paths_are_refused(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    (vcs_root / "evil").write_text("#!/bin/sh\necho pwned > /tmp/pwned\n")
    _commit_config(repo, git_path="./evil")
    with pytest.raises(ConfigError, match=r"\[vcs\] sets git_path") as exc:
        Repo.find(vcs_root)
    assert "secrets.toml" in str(exc.value)
    assert not Path("/tmp/pwned").exists()


def _vcs(root: Path, *args: str) -> None:
    subprocess.run(list(args), cwd=root, check=True, capture_output=True)


def _hostile_clone(src: Path, dst: Path, name: str, text: str) -> Path:
    """A dataset whose author committed `.tether/<name>` (taking it out of
    the committed `.tether/.gitignore` first), cloned to `dst` the way a
    user gets it: `git clone`, plus `jj git init --colocate` under jj."""
    jj = (src / ".jj").is_dir()
    Repo.init(src)
    ignore = src / ".tether" / ".gitignore"
    ignore.write_text(
        "".join(
            f"{line}\n"
            for line in ignore.read_text().splitlines()
            if line != f"/{name}"
        )
    )
    (src / ".tether" / name).write_text(text)
    if jj:
        _vcs(src, "jj", "commit", "-m", "dataset")
        _vcs(src, "jj", "bookmark", "set", "main", "-r", "@-")
    else:
        _vcs(src, "git", "add", "-A")
        _vcs(src, "git", "commit", "-qm", "dataset")
    _vcs(src, "git", "clone", "-q", "-b", "main", str(src), str(dst))
    if jj:
        _vcs(dst, "jj", "git", "init", "--colocate")
    return dst


@pytest.mark.parametrize("name", [SECRETS_FILENAME, WORKSPACE_FILENAME, OPS_FILENAME])
def test_a_clone_that_commits_a_per_checkout_file_is_refused(
    vcs_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """`.tether/.gitignore` is committed, so a dataset's author can commit
    the files it lists. A cloned `secrets.toml` naming `git_path` / `jj_path`
    ran that program on `tether status`; a cloned `workspace.toml` or
    `ops.jsonl` chose where writes go and which pins gc may release. Each is
    refused while the VCS tracks it -- before the secrets file is read, so
    the program never runs -- and the advice in the refusal works."""
    from tether.cli import app

    typer_testing = pytest.importorskip("typer.testing")
    jj = (vcs_root / ".jj").is_dir()
    outside = tmp_path_factory.mktemp("attacker")
    marker = outside / "PWNED"
    evil = outside / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    evil.chmod(0o755)
    text = {
        SECRETS_FILENAME: f'[vcs]\ngit_path = "{evil}"\njj_path = "{evil}"\n',
        WORKSPACE_FILENAME: 'workspace_id = "0123abcd"\nbookmark = "main"\n'
        '[working_refs]\ndb = "tether.ws.feedface.main"\n',
        OPS_FILENAME: '{"id": "0123456789ab", "at": "2026-01-01T00:00:00+00:00", '
        '"command": "commit", "result": {"pinned": {"db": "0a1b2c3d.1"}}}\n',
    }[name]
    clone = _hostile_clone(
        vcs_root, tmp_path_factory.mktemp("clone") / "ds", name, text
    )
    monkeypatch.chdir(clone)
    runner = typer_testing.CliRunner()
    r = runner.invoke(app, ["status"])
    assert r.exit_code != 0
    message = " ".join(r.output.split())
    assert f"{'jj' if jj else 'git'} tracks .tether/{name}" in message
    untrack = "jj file untrack" if jj else "git rm --cached"
    assert f"{untrack} .tether/{name}" in message
    with pytest.raises(ConfigError, match=rf"tracks \.tether/{name}"):
        Repo.find(clone)
    assert not marker.exists()

    # Following the refusal: a cloned secrets file goes; the others are
    # untracked as the message says, and stay on disk.
    if name == SECRETS_FILENAME:
        (clone / ".tether" / name).unlink()
    elif jj:
        _vcs(clone, "jj", "file", "untrack", f".tether/{name}")
    else:
        _vcs(clone, "git", "rm", "-q", "--cached", f".tether/{name}")
        _vcs(clone, "git", "commit", "-qm", "untrack")
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert not marker.exists()


def test_executables_come_from_secrets_or_env(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.init(vcs_root)
    git = shutil.which("git")
    assert git
    # The secrets file names the executable; it is untracked by construction.
    assert SECRETS_FILENAME in UNTRACKED_FILES
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text(f'[vcs]\ngit_path = "{git}"\n')
    secrets.chmod(0o600)
    found = Repo.find(vcs_root)
    assert found.secrets.vcs["git_path"] == git
    # A bogus TETHER_GIT is honoured -- and fails loudly -- when the file is silent.
    secrets.write_text("")
    monkeypatch.setenv("TETHER_GIT", str(vcs_root / "no-such-git"))
    if repo.vcs.kind == "git":
        with pytest.raises(Exception, match=r"no-such-git|No such file|not found"):
            Repo.find(vcs_root).vcs.current_rev()


def test_committed_backend_config_is_allowlisted(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    # A non-secret option in a screened table is fine...
    config = read_config(repo.root)
    config.backends["file"] = {"storage_options": {"region": "us-west-2"}}
    write_config(repo.root, config)
    Repo.find(vcs_root).backend_for("file")
    # ...an endpoint is not: credentials would be sent to it.
    config.backends["file"] = {"storage_options": {"endpoint": "http://evil"}}
    write_config(repo.root, config)
    with pytest.raises(ConfigError, match=r"storage_options\.endpoint") as exc:
        Repo.find(vcs_root).backend_for("file")
    assert "secrets.toml" in str(exc.value)
    # The table is an allowlist, not a pattern: a key the backend never named
    # is refused however innocuous it looks, so nothing slips through by
    # spelling (`proxy_url` would have passed a blocklist that forgot `proxy`).
    config.backends["file"] = {"storage_options": {"proxy_settings": "http://evil"}}
    write_config(repo.root, config)
    with pytest.raises(ConfigError, match=r"storage_options\.proxy_settings"):
        Repo.find(vcs_root).backend_for("file")
    # Keys outside the allowlist are refused whatever they hold.
    config.backends["ducklake"] = {"init_sql": ["CREATE SECRET ..."]}
    write_config(repo.root, config)
    with pytest.raises(ConfigError, match=r"\[backends.ducklake\] sets init_sql"):
        Repo.find(vcs_root).backend_for("ducklake")
    config.backends.pop("ducklake")
    config.backends["neon"] = {"api_url": "https://evil.example/api/v2"}
    write_config(repo.root, config)
    with pytest.raises(ConfigError, match=r"\[backends.neon\] sets api_url"):
        Repo.find(vcs_root).backend_for("neon")


def test_secrets_file_supplies_what_the_committed_file_may_not(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text(
        "[backends.ducklake]\n"
        'init_sql = ["SELECT 1"]\n'
        "[backends.file]\n"
        'storage_options = { endpoint = "http://localhost:9000" }\n'
    )
    secrets.chmod(0o600)
    found = Repo.find(vcs_root)
    ducklake = cast(Any, found.backend_for("ducklake"))
    assert ducklake._config["init_sql"] == ["SELECT 1"]
    file = cast(Any, found.backend_for("file"))
    assert file._config["storage_options"] == {"endpoint": "http://localhost:9000"}
    assert repo.secrets.backends == {}  # the earlier Repo read no file


def test_two_icechunk_objects_two_credential_sets(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-URI entry and a per-object entry resolve to explicit credentials
    for their objects; an object with no entry falls through to the
    environment (`from_env=True`), as it always did."""
    import icechunk as ic

    repo = Repo.init(vcs_root)
    repo.add("a", "icechunk", {"uri": "s3://bucket-a/repo"})
    repo.add("b", "icechunk", {"uri": "s3://bucket-b/repo"})
    repo.add("c", "icechunk", {"uri": "s3://bucket-c/repo"})
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text(
        '[uris."s3://bucket-a/"]\n'
        'access_key_id = "AKIA_A"\n'
        'secret_access_key = "sk-a"\n'
        'endpoint_url = "http://a.local:9000"\n'
        '[objects."b"]\n'
        'access_key_id = "AKIA_B"\n'
        'secret_access_key = "sk-b"\n'
        'session_token = "tok-b"\n'
        'region = "eu-west-1"\n'
    )
    secrets.chmod(0o600)
    calls: list[dict] = []
    monkeypatch.setattr(ic, "s3_storage", lambda **kw: calls.append(kw) or object())
    found = Repo.find(vcs_root)
    backend = cast(Any, found.backend_for("icechunk"))
    for key in ("a", "b", "c"):
        backend._storage(found.objects[key].locator)
    a, b, c = calls
    assert a["access_key_id"] == "AKIA_A" and a["endpoint_url"] == "http://a.local:9000"
    assert "from_env" not in a
    assert b["access_key_id"] == "AKIA_B" and b["session_token"] == "tok-b"
    assert b["region"] == "eu-west-1"
    assert c.get("from_env") is True and "access_key_id" not in c
    # Nothing from the secrets file reached the manifests.
    for key in ("a", "b", "c"):
        assert set(found.objects[key].locator) == {"uri"}


def test_per_object_secrets_follow_objects_added_after_the_backend_was_built(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[objects."<key>"]` names a key; which store that key means is known
    only once the object exists. A backend built before the `add` -- the
    common case inside one process: `add` then `commit`, or `Repo.create`
    -- must still see the entry for the new object."""
    import icechunk as ic

    Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text(
        '[objects."b"]\naccess_key_id = "AKIA_B"\nsecret_access_key = "sk-b"\n'
    )
    secrets.chmod(0o600)
    calls: list[dict] = []
    monkeypatch.setattr(ic, "s3_storage", lambda **kw: calls.append(kw) or object())
    repo = Repo.find(vcs_root)
    repo.add("a", "icechunk", {"uri": "s3://bucket-a/repo"})
    backend = cast(Any, repo.backend_for("icechunk"))  # built: `b` not registered
    repo.add("b", "icechunk", {"uri": "s3://bucket-b/repo"})
    assert repo.backend_for("icechunk") is backend  # same instance, fresh rules
    backend._storage(repo.objects["b"].locator)
    (b,) = calls
    assert b["access_key_id"] == "AKIA_B" and "from_env" not in b
    # Removing the object drops its rule again; `a` never had one.
    repo.remove("b")
    repo.backend_for("icechunk")
    assert "s3://bucket-b/repo" not in backend._secret_rules
    backend._storage(repo.objects["a"].locator)
    assert calls[-1].get("from_env") is True


def test_secret_rules_are_recomputed_only_when_the_objects_change(
    vcs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`backend_for` runs in fingerprint fan-outs and history walks; the
    per-object rules are rebuilt on a change to `objects`, not per call."""
    Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text('[objects."a"]\naccess_key_id = "AKIA_A"\n')
    secrets.chmod(0o600)
    repo = Repo.find(vcs_root)
    calls = 0
    real = repo._secret_rules_for

    def counting(kind: str, backend: Any) -> dict:
        nonlocal calls
        calls += 1
        return real(kind, backend)

    monkeypatch.setattr(repo, "_secret_rules_for", counting)
    repo.backend_for("icechunk")  # first build
    for _ in range(50):
        repo.backend_for("icechunk")
    assert calls == 2  # the build, then one check for the unchanged objects
    # `add` reloads `objects` under the writer lock and then changes it: two
    # real changes, at most two recomputes -- and then none again.
    repo.add("a", "icechunk", {"uri": "s3://bucket-a/repo"})
    b = cast(Any, repo.backend_for("icechunk"))
    assert calls <= 4 and "s3://bucket-a/repo" in b._secret_rules
    after = calls
    for _ in range(50):
        repo.backend_for("icechunk")
    assert calls == after


def test_lax_permissions_on_the_secrets_file_warn(vcs_root: Path) -> None:
    Repo.init(vcs_root)
    secrets = vcs_root / ".tether" / SECRETS_FILENAME
    secrets.write_text("[vcs]\n")
    secrets.chmod(0o644)
    with pytest.warns(UserWarning, match="readable by other users"):
        Repo.find(vcs_root)
    secrets.chmod(0o600)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Repo.find(vcs_root)


def test_iceberg_locator_may_not_carry_catalog_endpoints(vcs_root: Path) -> None:
    repo = Repo.init(vcs_root)
    repo.add(
        "t",
        "iceberg",
        {
            "identifier": "db.t",
            "catalog": {"uri": "https://evil.example", "token": "x"},
        },
    )
    with pytest.raises(BackendError, match=r"catalog\.token, catalog\.uri"):
        cast(Any, repo.backend_for("iceberg"))._catalog(repo.objects["t"].locator)
