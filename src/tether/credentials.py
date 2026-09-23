"""Turn credential *references* into the options a storage library takes.

`.tether/secrets.toml` names credentials rather than holding them where it
can: `profile` (an AWS shared-config profile), `role_arn` (assumed through
STS), or, when nothing else will do, literal `access_key_id` /
`secret_access_key` / `session_token`. Backends call :func:`aws_credentials`
to resolve whichever is present into static keys they can hand to Icechunk,
obstore, delta-rs, or Lance -- one identity per object, which the ambient
environment (`from_env`) cannot express for two stores in one command.

Resolution needs `boto3` only when a profile or role is named; a checkout
that uses the environment or literal keys never imports it. What it resolves
is cached per set of options, with the expiry STS or botocore reports: a
long-lived `Repo` neither assumes a role once per operation nor keeps keys
that expired an hour ago.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from tether.errors import ConfigError

__all__ = ["aws_credentials", "storage_options"]

_CREDENTIAL_KEYS = ("access_key_id", "secret_access_key", "session_token")

REFRESH_MARGIN = 300.0
"""Seconds before expiry at which cached credentials are resolved again. A
request signed with keys about to expire can be refused mid-flight; a
fresh set well ahead of that costs one STS call."""

_cache: dict[str, tuple[dict[str, str], float | None]] = {}
"""Canonical options -> (credentials, expiry as epoch seconds, or `None` for
keys that do not expire)."""
_lock = threading.Lock()


def _now() -> float:
    """The clock expiries are compared against (tests replace it)."""
    return time.time()


def _cache_key(options: Mapping[str, Any]) -> str:
    return json.dumps(options, sort_keys=True, default=str)


def _epoch(value: Any) -> float | None:
    """An expiry as epoch seconds: STS hands a datetime, botocore's refreshable
    credentials keep one in `_expiry_time`; `None` means the keys are static."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, int | float):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def aws_credentials(options: Mapping[str, Any]) -> dict[str, str] | None:
    """Static AWS credentials for `options`, or `None` to use the environment.

    Precedence: literal keys, then `role_arn` (assumed from `profile` or the
    default chain), then `profile`. Returns `access_key_id`,
    `secret_access_key`, and `session_token` (when the source has one).

    A profile or role is resolved once per set of options and served from a
    cache until :data:`REFRESH_MARGIN` seconds before the credentials expire
    (static profile keys: indefinitely).

    Raises:
        ConfigError: A profile or role is named but `boto3` is not installed,
            or the credentials could not be resolved.
    """
    if options.get("access_key_id"):
        out = {k: str(options[k]) for k in _CREDENTIAL_KEYS if options.get(k)}
        if "secret_access_key" not in out:
            raise ConfigError("access_key_id set without secret_access_key")
        return out
    profile = options.get("profile")
    role = options.get("role_arn")
    if not profile and not role:
        return None
    key = _cache_key(options)
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        creds, expires = hit
        if expires is None or expires - _now() > REFRESH_MARGIN:
            return dict(creds)
    creds, expires = _resolve(options, profile, role)
    with _lock:
        _cache[key] = (creds, expires)
    return dict(creds)


def _resolve(
    options: Mapping[str, Any], profile: Any, role: Any
) -> tuple[dict[str, str], float | None]:
    """Ask boto3 for `profile` / `role`: the keys and when they expire."""
    try:
        import boto3  # ty: ignore[unresolved-import]  # optional, resolved lazily
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ConfigError(
            "resolving an AWS profile or role_arn needs boto3 "
            "(`pip install boto3`), or set literal credentials"
        ) from exc
    try:
        session = boto3.Session(
            profile_name=str(profile) if profile else None,
            region_name=str(options["region"]) if options.get("region") else None,
        )
        if role:
            sts = session.client("sts")
            got = sts.assume_role(
                RoleArn=str(role),
                RoleSessionName=str(options.get("role_session_name", "tether")),
            )["Credentials"]
            return {
                "access_key_id": got["AccessKeyId"],
                "secret_access_key": got["SecretAccessKey"],
                "session_token": got["SessionToken"],
            }, _epoch(got.get("Expiration"))
        creds = session.get_credentials()
        if creds is None:
            raise ConfigError(f"AWS profile {profile!r} yields no credentials")
        frozen = creds.get_frozen_credentials()
        out = {
            "access_key_id": frozen.access_key,
            "secret_access_key": frozen.secret_key,
        }
        if frozen.token:
            out["session_token"] = frozen.token
        # Refreshable credentials (SSO, a process provider, a role in the
        # profile) know when they expire; static keys carry no such time.
        return out, _epoch(getattr(creds, "_expiry_time", None))
    except ConfigError:
        raise
    except Exception as exc:  # botocore's many exception classes
        raise ConfigError(f"could not resolve AWS credentials: {exc}") from exc


def storage_options(options: Mapping[str, Any]) -> dict[str, str]:
    """`options` as the `AWS_*` storage-option keys obstore, delta-rs, and
    Lance understand: resolved credentials plus `endpoint_url` and `region`.
    Empty when the object has no secrets entry."""
    out: dict[str, str] = {}
    creds = aws_credentials(options)
    if creds:
        out["AWS_ACCESS_KEY_ID"] = creds["access_key_id"]
        out["AWS_SECRET_ACCESS_KEY"] = creds["secret_access_key"]
        if creds.get("session_token"):
            out["AWS_SESSION_TOKEN"] = creds["session_token"]
    if options.get("endpoint_url"):
        out["AWS_ENDPOINT_URL"] = str(options["endpoint_url"])
    if options.get("region"):
        out["AWS_REGION"] = str(options["region"])
    return out
