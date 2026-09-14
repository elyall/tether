"""Turn credential *references* into the options a storage library takes.

`.tether/secrets.toml` names credentials rather than holding them where it
can: `profile` (an AWS shared-config profile), `role_arn` (assumed through
STS), or, when nothing else will do, literal `access_key_id` /
`secret_access_key` / `session_token`. Backends call :func:`aws_credentials`
to resolve whichever is present into static keys they can hand to Icechunk,
obstore, delta-rs, or Lance -- one identity per object, which the ambient
environment (`from_env`) cannot express for two stores in one command.

Resolution needs `boto3` only when a profile or role is named; a checkout
that uses the environment or literal keys never imports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tether.errors import ConfigError

__all__ = ["aws_credentials", "storage_options"]

_CREDENTIAL_KEYS = ("access_key_id", "secret_access_key", "session_token")


def aws_credentials(options: Mapping[str, Any]) -> dict[str, str] | None:
    """Static AWS credentials for `options`, or `None` to use the environment.

    Precedence: literal keys, then `role_arn` (assumed from `profile` or the
    default chain), then `profile`. Returns `access_key_id`,
    `secret_access_key`, and `session_token` (when the source has one).

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
            }
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
        return out
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
