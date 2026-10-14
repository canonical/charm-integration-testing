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


def provider_from_environment(
    environ: Mapping[str, str], session: requests.Session, prefix: str = ""
) -> S3Provider | None:
    """S3_OS_STORAGE_URL opts into Swift without consuming cloud-bootstrap credentials."""

    def get(name: str) -> str | None:
        return environ.get(f"{prefix}{name}")

    def require(name: str) -> str:
        value = get(name)
        if not value:
            raise ValueError(f"Missing S3 credential variable: {prefix}{name}")
        return value

    namespace = get("S3_BUCKET_NAMESPACE") or "local"
    region = get("S3_REGION") or "us-east-1"
    if get("S3_OS_STORAGE_URL"):
        missing = [f"{prefix}{name}" for name in _SWIFT_VARIABLES if not get(name)]
        if missing:
            raise ValueError("Missing Swift credential variables: " + ", ".join(missing))
        missing_cleanup = [f"{prefix}{name}" for name in _S3_CLEANUP_VARIABLES if not get(name)]
        if missing_cleanup:
            raise ValueError("Missing S3 cleanup credential variables: " + ", ".join(missing_cleanup))
        credentials = SwiftCredentials(
            auth_url=require("S3_OS_AUTH_URL"),
            username=require("S3_OS_USERNAME"),
            password=require("S3_OS_PASSWORD"),
            user_domain_name=require("S3_OS_USER_DOMAIN_NAME"),
            project_name=require("S3_OS_PROJECT_NAME"),
            project_domain_name=require("S3_OS_PROJECT_DOMAIN_NAME"),
            storage_url=require("S3_OS_STORAGE_URL"),
        )
        endpoint = get("S3_ENDPOINT") or credentials.s3_endpoint()
        return SwiftS3Provider(
            endpoint,
            namespace,
            KeystoneClient(credentials, session),
            S3Credentials(require("S3_CLEANUP_ACCESS_KEY"), require("S3_CLEANUP_SECRET_KEY")),
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
                require("S3_CLEANUP_ACCESS_KEY"),
                require("S3_CLEANUP_SECRET_KEY"),
            ),
        )
    if any(get(name) for name in _S3_VARIABLES):
        missing = [f"{prefix}{name}" for name in _S3_VARIABLES if not get(name)]
        if missing:
            raise ValueError("Missing S3 credential variables: " + ", ".join(missing))
        return StaticS3Provider(
            require("S3_ENDPOINT"),
            namespace,
            S3Credentials(require("S3_ACCESS_KEY"), require("S3_SECRET_KEY")),
            region=region,
            identity=_provider_identity(
                "static",
                require("S3_ENDPOINT"),
                namespace,
                region,
                require("S3_ACCESS_KEY"),
                require("S3_SECRET_KEY"),
                get("S3_CLEANUP_ACCESS_KEY") or "",
                get("S3_CLEANUP_SECRET_KEY") or "",
            ),
            cleanup_credentials=(
                S3Credentials(require("S3_CLEANUP_ACCESS_KEY"), require("S3_CLEANUP_SECRET_KEY"))
                if any(get(name) for name in _S3_CLEANUP_VARIABLES)
                else None
            ),
        )
    return None
