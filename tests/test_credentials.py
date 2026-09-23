"""`aws_credentials`: references resolved through boto3, cached until they
are about to expire. boto3 is not installed here; a fake module stands in."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from tether import credentials
from tether.credentials import REFRESH_MARGIN, aws_credentials

NOW = 1_700_000_000.0
HOUR = 3600.0


class _Frozen:
    def __init__(self, n: int, token: str | None) -> None:
        self.access_key = f"AKIA{n}"
        self.secret_key = f"sk{n}"
        self.token = token


class _Credentials:
    """What `Session.get_credentials()` returns: refreshable ones carry an
    `_expiry_time`, static ones do not."""

    def __init__(self, n: int, expiry: datetime | None) -> None:
        self._n = n
        if expiry is not None:
            self._expiry_time = expiry

    def get_frozen_credentials(self) -> _Frozen:
        return _Frozen(self._n, "tok" if hasattr(self, "_expiry_time") else None)


class _FakeBoto3:
    def __init__(self) -> None:
        self.assumed: list[dict[str, Any]] = []
        self.sessions: list[tuple[str | None, str | None]] = []
        self.clock = NOW
        self.profile_expiry: datetime | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self
        module = cast(Any, types.ModuleType("boto3"))

        class Session:
            def __init__(
                self, profile_name: str | None = None, region_name: str | None = None
            ) -> None:
                fake.sessions.append((profile_name, region_name))

            def client(self, name: str) -> Any:
                assert name == "sts"
                return _STS()

            def get_credentials(self) -> _Credentials:
                return _Credentials(len(fake.sessions), fake.profile_expiry)

        class _STS:
            def assume_role(self, **kw: Any) -> dict[str, Any]:
                fake.assumed.append(kw)
                n = len(fake.assumed)
                return {
                    "Credentials": {
                        "AccessKeyId": f"ASIA{n}",
                        "SecretAccessKey": f"sk{n}",
                        "SessionToken": f"tok{n}",
                        "Expiration": datetime.fromtimestamp(fake.clock + HOUR, UTC),
                    }
                }

        module.Session = Session
        monkeypatch.setitem(sys.modules, "boto3", module)
        monkeypatch.setattr(credentials, "_cache", {})
        monkeypatch.setattr(credentials, "_now", lambda: fake.clock)


@pytest.fixture
def boto3(monkeypatch: pytest.MonkeyPatch) -> _FakeBoto3:
    fake = _FakeBoto3()
    fake.install(monkeypatch)
    return fake


def test_literal_keys_never_touch_boto3(boto3: _FakeBoto3) -> None:
    creds = aws_credentials({"access_key_id": "AKIA", "secret_access_key": "s"})
    assert creds == {"access_key_id": "AKIA", "secret_access_key": "s"}
    assert aws_credentials({"region": "us-east-1"}) is None
    assert boto3.sessions == [] and boto3.assumed == []


def test_assumed_role_is_cached_until_near_expiry(boto3: _FakeBoto3) -> None:
    """One STS call serves every operation until five minutes before the
    credentials expire; then a fresh set is assumed, and callers that
    compare keys (the icechunk and file store caches) see the change."""
    options = {"role_arn": "arn:aws:iam::1:role/r", "region": "eu-west-1"}
    first = aws_credentials(options)
    assert first is not None and first["access_key_id"] == "ASIA1"
    assert first["session_token"] == "tok1"
    assert aws_credentials(options) == first
    assert aws_credentials(dict(options)) == first  # equal options: one entry
    assert len(boto3.assumed) == 1
    assert boto3.assumed[0] == {
        "RoleArn": "arn:aws:iam::1:role/r",
        "RoleSessionName": "tether",
    }

    boto3.clock = NOW + HOUR - REFRESH_MARGIN - 1
    assert aws_credentials(options) == first  # still outside the margin
    boto3.clock = NOW + HOUR - REFRESH_MARGIN + 1
    second = aws_credentials(options)
    assert second is not None and second["access_key_id"] == "ASIA2"
    assert len(boto3.assumed) == 2
    assert aws_credentials(options) == second  # cached again, with its own expiry

    # Another role (or the same role for another region) is another entry.
    other = aws_credentials({"role_arn": "arn:aws:iam::1:role/other"})
    assert other is not None and other["access_key_id"] == "ASIA3"
    assert aws_credentials(options) == second


def test_static_profile_keys_are_cached_indefinitely(boto3: _FakeBoto3) -> None:
    options = {"profile": "lab"}
    first = aws_credentials(options)
    assert first == {"access_key_id": "AKIA1", "secret_access_key": "sk1"}
    boto3.clock = NOW + 365 * 24 * HOUR
    assert aws_credentials(options) == first
    assert boto3.sessions == [("lab", None)]


def test_refreshable_profile_credentials_are_resolved_again_near_expiry(
    boto3: _FakeBoto3,
) -> None:
    boto3.profile_expiry = datetime.fromtimestamp(NOW + HOUR, UTC)
    options = {"profile": "sso", "region": "us-west-2"}
    first = aws_credentials(options)
    assert first == {
        "access_key_id": "AKIA1",
        "secret_access_key": "sk1",
        "session_token": "tok",
    }
    boto3.clock = NOW + HOUR / 2
    assert aws_credentials(options) == first
    boto3.clock = NOW + HOUR - REFRESH_MARGIN / 2
    boto3.profile_expiry = datetime.fromtimestamp(boto3.clock + HOUR, UTC)
    second = aws_credentials(options)
    assert second is not None and second["access_key_id"] == "AKIA2"
    assert boto3.sessions == [("sso", "us-west-2")] * 2


def test_epoch_accepts_what_boto3_and_botocore_hand_out() -> None:
    from tether.credentials import _epoch

    assert _epoch(None) is None
    assert _epoch(datetime.fromtimestamp(NOW, UTC)) == NOW
    assert _epoch(datetime.fromtimestamp(NOW, UTC).replace(tzinfo=None)) == NOW
    assert _epoch("2023-11-14T22:13:20Z") == NOW
    assert _epoch(NOW) == NOW
