# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping

from .provider import S3Credentials, S3Provider
from .storage import StaticS3Provider

_S3_VARIABLES = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY")
_S3_CLEANUP_VARIABLES = ("S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY")
_IDENTITY_KEY = secrets.token_bytes(32)


def _provider_identity(*values: str) -> str:
    """Return an opaque identity for deduplicating identical provider configurations in this process."""
    serialized = json.dumps(values, separators=(",", ":"))
    return hmac.new(_IDENTITY_KEY, serialized.encode(), hashlib.sha256).hexdigest()


def _get_environment_value(environ: Mapping[str, str], name: str, prefix: str) -> str | None:
    return environ.get(f"{prefix}{name}")


def _require_environment_value(environ: Mapping[str, str], name: str, prefix: str) -> str:
    value = _get_environment_value(environ, name, prefix)
    if not value:
        raise ValueError(f"Missing S3 credential variable: {prefix}{name}")
    return value


def provider_from_environment(environ: Mapping[str, str], prefix: str = "") -> S3Provider | None:
    """Construct a static S3 provider from explicit configuration values."""
    namespace = _get_environment_value(environ, "S3_BUCKET_NAMESPACE", prefix) or "local"
    region = _get_environment_value(environ, "S3_REGION", prefix) or "us-east-1"
    if any(_get_environment_value(environ, name, prefix) for name in _S3_VARIABLES):
        missing = [f"{prefix}{name}" for name in _S3_VARIABLES if not _get_environment_value(environ, name, prefix)]
        if missing:
            raise ValueError("Missing S3 credential variables: " + ", ".join(missing))
        return StaticS3Provider(
            _require_environment_value(environ, "S3_ENDPOINT", prefix),
            namespace,
            S3Credentials(
                _require_environment_value(environ, "S3_ACCESS_KEY", prefix),
                _require_environment_value(environ, "S3_SECRET_KEY", prefix),
            ),
            region=region,
            identity=_provider_identity(
                "static",
                _require_environment_value(environ, "S3_ENDPOINT", prefix),
                namespace,
                region,
                _require_environment_value(environ, "S3_ACCESS_KEY", prefix),
                _require_environment_value(environ, "S3_SECRET_KEY", prefix),
                _get_environment_value(environ, "S3_CLEANUP_ACCESS_KEY", prefix) or "",
                _get_environment_value(environ, "S3_CLEANUP_SECRET_KEY", prefix) or "",
            ),
            cleanup_credentials=(
                S3Credentials(
                    _require_environment_value(environ, "S3_CLEANUP_ACCESS_KEY", prefix),
                    _require_environment_value(environ, "S3_CLEANUP_SECRET_KEY", prefix),
                )
                if any(_get_environment_value(environ, name, prefix) for name in _S3_CLEANUP_VARIABLES)
                else None
            ),
        )
    return None
