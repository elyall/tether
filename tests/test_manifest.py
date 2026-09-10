from __future__ import annotations

from pathlib import Path

from tether.manifest import (
    ObjectManifest,
    Pin,
    Policy,
    RepoConfig,
    WorkspaceState,
    compute_pin_id,
    key_to_relpath,
    manifest_hash,
    pin_dataset,
    ref_for_pin,
    relpath_to_key,
    slugify_key,
    working_ref_name,
)


def test_object_manifest_round_trip() -> None:
    m = ObjectManifest(
        key="zarr/imaging",
        kind="icechunk",
        locator={"uri": "s3://bucket/imaging.zarr.icechunk"},
        policy=Policy(write="fork", file="immutable", pin="native"),
        state={"snapshot_id": "abc123"},
        pin=Pin("deadbeef1234", "tether.deadbeef1234"),
        captured_at="2026-09-04T00:00:00+00:00",
        recoverable=True,
    )
    assert ObjectManifest.from_toml(m.to_toml()) == m


def test_object_manifest_uncommitted_round_trip() -> None:
    m = ObjectManifest(key="db", kind="neon", locator={"project_id": "p"})
    back = ObjectManifest.from_toml(m.to_toml())
    assert back.state is None and back.pin is None
    assert back == m


def test_repo_config_round_trip() -> None:
    c = RepoConfig(
        snapshot_auto=False,
        verify_on_status=True,
        new_auto_fork=True,
        defaults=Policy(write="direct"),
        vcs={"jj_path": "/x/jj"},
        backends={"neon": {"api_key_env": "NEON_API_KEY"}},
    )
    assert RepoConfig.from_toml(c.to_toml()) == c


def test_workspace_round_trip() -> None:
    ws = WorkspaceState(
        base_states={"zarr/imaging": {"snapshot_id": "abc"}},
        working_refs={"zarr/imaging": "tether.ws.abcd1234.zarr-imaging"},
        last_snapshot={"zarr/imaging": {"snapshot_id": "abc"}},
        last_snapshot_at="2026-09-04T00:00:00+00:00",
    )
    assert WorkspaceState.from_toml(ws.to_toml()) == ws


def test_pin_id_deterministic_and_state_sensitive() -> None:
    a = compute_pin_id(
        "icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "1"}, "0a1b2c3d"
    )
    b = compute_pin_id(
        "icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "1"}, "0a1b2c3d"
    )
    c = compute_pin_id(
        "icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "2"}, "0a1b2c3d"
    )
    d = compute_pin_id(
        "icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "1"}, "ffffffff"
    )
    assert a == b and a != c and a != d
    assert a.startswith("0a1b2c3d.") and len(a) == 8 + 1 + 16
    assert pin_dataset(a) == "0a1b2c3d" and pin_dataset(d) == "ffffffff"
    assert pin_dataset("ws.0a1b2c3d.x") is None and pin_dataset("abc") is None


def test_ref_and_slug_helpers() -> None:
    from tether.manifest import working_ref_dataset, working_ref_workspace

    assert ref_for_pin("abc") == "tether.abc"
    assert slugify_key("zarr/imaging") == "zarr-imaging"
    name = working_ref_name("0a1b2c3d", "abcd1234efgh", "zarr/imaging")
    assert name.startswith("tether.ws.0a1b2c3d.abcd1234.zarr-imaging-")
    assert "." not in name.removeprefix("tether.ws.0a1b2c3d.abcd1234.")
    assert working_ref_workspace(name) == "abcd1234"
    assert working_ref_dataset(name) == "0a1b2c3d"
    assert working_ref_dataset("tether.ws.not-hex.abcd1234.zarr-x") is None
    assert working_ref_dataset("tether.ws.abcd1234.zarr-x") is None  # pre-namespace
    assert working_ref_workspace("tether.ws.abcd1234.zarr-x") is None
    assert working_ref_workspace("feature-x") is None
    # Keys that slugify identically still get distinct branches.
    assert name != working_ref_name("0a1b2c3d", "abcd1234efgh", "zarr-imaging")
    assert slugify_key("zarr/imaging") == slugify_key("zarr-imaging")
    assert working_ref_name("0a1b2c3d", "abcd1234efgh", "zarr/imaging") == name
    # Another dataset, same workspace id and key: a different branch.
    assert working_ref_name("ffffffff", "abcd1234efgh", "zarr/imaging") != name


def test_key_path_mapping() -> None:
    rel = key_to_relpath("zarr/imaging")
    assert rel == Path("objects/zarr/imaging.toml")
    assert relpath_to_key(Path("zarr/imaging.toml")) == "zarr/imaging"


def test_manifest_hash_order_independent() -> None:
    m1 = ObjectManifest(key="a", kind="file", locator={"uri": "/a"})
    m2 = ObjectManifest(key="b", kind="file", locator={"uri": "/b"})
    assert manifest_hash({"a": m1, "b": m2}) == manifest_hash({"b": m2, "a": m1})
    m2b = ObjectManifest(key="b", kind="file", locator={"uri": "/c"})
    assert manifest_hash({"a": m1, "b": m2}) != manifest_hash({"a": m1, "b": m2b})
