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
        defaults=Policy(write="track"),
        vcs={"jj_path": "/x/jj"},
        backends={"neon": {"api_key_env": "NEON_API_KEY"}},
    )
    assert RepoConfig.from_toml(c.to_toml()) == c


def test_workspace_round_trip() -> None:
    ws = WorkspaceState(
        base="hash",
        working_refs={"zarr/imaging": "tether.ws.abcd1234.zarr-imaging"},
        last_snapshot={"zarr/imaging": {"snapshot_id": "abc"}},
        last_snapshot_at="2026-09-04T00:00:00+00:00",
    )
    assert WorkspaceState.from_toml(ws.to_toml()) == ws


def test_pin_id_deterministic_and_state_sensitive() -> None:
    a = compute_pin_id("icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "1"})
    b = compute_pin_id("icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "1"})
    c = compute_pin_id("icechunk", {"uri": "s3://b/x"}, {"snapshot_id": "2"})
    assert a == b and a != c and len(a) == 12


def test_ref_and_slug_helpers() -> None:
    assert ref_for_pin("abc") == "tether.abc"
    assert slugify_key("zarr/imaging") == "zarr-imaging"
    assert "." not in working_ref_name("abcd1234efgh", "zarr/imaging").split("tether.")[
        1
    ].replace("ws.", "").replace("abcd1234.", "")


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
