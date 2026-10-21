# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping

import requests

from .provider import S3Credentials, S3Provider
from .storage import StaticS3Provider
from .swift import KeystoneClient, SwiftCredentials, SwiftS3Provider

_SWIFT_VARIABLES = (
    "S3_OS_AUTH_URL",
    "S3_OS_USERNAME",
    "S3_OS_PASSWORD",
    "S3_OS_USER_DOMAIN_NAME",
    "S3_OS_PROJECT_NAME",
    "S3_OS_PROJECT_DOMAIN_NAME",
    "S3_OS_STORAGE_URL",
)
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


def provider_from_environment(
    environ: Mapping[str, str], session: requests.Session | None = None, prefix: str = ""
) -> S3Provider | None:
    """Construct a provider from environment values; Swift requires a requests session."""
    namespace = _get_environment_value(environ, "S3_BUCKET_NAMESPACE", prefix) or "local"
    region = _get_environment_value(environ, "S3_REGION", prefix) or "us-east-1"
    if _get_environment_value(environ, "S3_OS_STORAGE_URL", prefix):
        missing = [f"{prefix}{name}" for name in _SWIFT_VARIABLES if not _get_environment_value(environ, name, prefix)]
        if missing:
            raise ValueError("Missing Swift credential variables: " + ", ".join(missing))
        missing_cleanup = [
            f"{prefix}{name}" for name in _S3_CLEANUP_VARIABLES if not _get_environment_value(environ, name, prefix)
        ]
        if missing_cleanup:
            raise ValueError("Missing S3 cleanup credential variables: " + ", ".join(missing_cleanup))
        if session is None:
            raise ValueError("A requests session is required when configuring a Swift S3 provider")
        credentials = SwiftCredentials(
            auth_url=_require_environment_value(environ, "S3_OS_AUTH_URL", prefix),
            username=_require_environment_value(environ, "S3_OS_USERNAME", prefix),
            password=_require_environment_value(environ, "S3_OS_PASSWORD", prefix),
            user_domain_name=_require_environment_value(environ, "S3_OS_USER_DOMAIN_NAME", prefix),
            project_name=_require_environment_value(environ, "S3_OS_PROJECT_NAME", prefix),
            project_domain_name=_require_environment_value(environ, "S3_OS_PROJECT_DOMAIN_NAME", prefix),
            storage_url=_require_environment_value(environ, "S3_OS_STORAGE_URL", prefix),
        )
        endpoint = _get_environment_value(environ, "S3_ENDPOINT", prefix) or credentials.s3_endpoint()
        return SwiftS3Provider(
            endpoint,
            namespace,
            KeystoneClient(credentials, session),
            S3Credentials(
                _require_environment_value(environ, "S3_CLEANUP_ACCESS_KEY", prefix),
                _require_environment_value(environ, "S3_CLEANUP_SECRET_KEY", prefix),
            ),
            region=region,
            identity=_provider_identity(
                "swift",
                endpoint,
                namespace,
                region,
                credentials.auth_url,
                credentials.username,
                credentials.password,
                credentials.user_domain_name,
                credentials.project_name,
                credentials.project_domain_name,
                credentials.storage_url,
                _require_environment_value(environ, "S3_CLEANUP_ACCESS_KEY", prefix),
                _require_environment_value(environ, "S3_CLEANUP_SECRET_KEY", prefix),
            ),
        )
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
