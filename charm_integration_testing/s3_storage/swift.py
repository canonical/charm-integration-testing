# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, SecretStr

from .provider import CredentialProvider, S3Credentials
from .storage import S3Storage


@dataclass(frozen=True)
class SwiftCredentials:
    auth_url: str
    username: str
    password: str = field(repr=False)
    user_domain_name: str
    project_name: str
    project_domain_name: str
    storage_url: str

    def s3_endpoint(self) -> str:
        """RGW exposes S3 at the origin of its /swift/v1 account endpoint."""
        url = urlsplit(self.storage_url)
        if (
            re.fullmatch(r"/swift/v1(?:/AUTH_[^/]+)?/?", url.path) is None
            or not url.hostname
            or url.scheme not in ("http", "https")
        ):
            raise ValueError("Set S3_ENDPOINT explicitly when S3_OS_STORAGE_URL is not an RGW /swift/v1 URL")
        return f"{url.scheme}://{url.netloc}"


class _Identity(BaseModel):
    id: str


class _Token(BaseModel):
    user: _Identity
    project: _Identity


class _TokenResponse(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    token: _Token


class _Credential(BaseModel):
    access: SecretStr
    secret: SecretStr


class _CredentialResponse(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    credential: _Credential


class KeystoneClient:
    """Issue and revoke S3 credentials scoped to the authenticated project."""

    def __init__(self, credentials: SwiftCredentials, session: requests.Session, timeout: float = 30) -> None:
        self.credentials = credentials
        self.session = session
        self.timeout = timeout
        self._url = credentials.auth_url.rstrip("/")
        if not self._url.endswith("/v3"):
            self._url += "/v3"

    def _authenticate(self) -> tuple[str, _Token]:
        response = self.session.post(
            f"{self._url}/auth/tokens",
            json={
                "auth": {
                    "identity": {
                        "methods": ["password"],
                        "password": {
                            "user": {
                                "name": self.credentials.username,
                                "password": self.credentials.password,
                                "domain": {"name": self.credentials.user_domain_name},
                            }
                        },
                    },
                    "scope": {
                        "project": {
                            "name": self.credentials.project_name,
                            "domain": {"name": self.credentials.project_domain_name},
                        }
                    },
                }
            },
            timeout=self.timeout,
        )
        self._check_response(response, "authenticate")
        identity = _TokenResponse.model_validate(response.json()).token
        return response.headers["X-Subject-Token"], identity

    @staticmethod
    def _check_response(response: requests.Response, operation: str) -> None:
        if not 200 <= response.status_code < 300:
            # Do not include response bodies or request payloads containing credentials.
            raise RuntimeError(f"Keystone failed to {operation} (HTTP {response.status_code})")

    def create_credentials(self) -> S3Credentials:
        token, identity = self._authenticate()
        response = self.session.post(
            f"{self._url}/users/{identity.user.id}/credentials/OS-EC2",
            headers={"X-Auth-Token": token},
            json={"tenant_id": identity.project.id},
            timeout=self.timeout,
        )
        self._check_response(response, "create EC2 credentials")
        credential = _CredentialResponse.model_validate(response.json()).credential
        return S3Credentials(credential.access.get_secret_value(), credential.secret.get_secret_value())

    def delete_credentials(self, access_key: str) -> None:
        # Teardown may happen hours after provisioning; obtain a fresh token.
        token, identity = self._authenticate()
        response = self.session.delete(
            f"{self._url}/users/{identity.user.id}/credentials/OS-EC2/{access_key}",
            headers={"X-Auth-Token": token},
            timeout=self.timeout,
        )
        if response.status_code != 404:
            self._check_response(response, "delete EC2 credentials")


class SwiftCredentialProvider(CredentialProvider):
    """Per-bucket Keystone EC2 keys, with a persistent identity reserved for cleanup."""

    per_bucket_credentials = True

    def __init__(self, keystone: KeystoneClient, cleanup_credentials: S3Credentials) -> None:
        self.keystone = keystone
        self._cleanup_credentials = cleanup_credentials

    @property
    def cleanup_credentials(self) -> S3Credentials:
        return self._cleanup_credentials

    def issue(self) -> S3Credentials:
        return self.keystone.create_credentials()

    def revoke(self, access_key: str) -> None:
        self.keystone.delete_credentials(access_key)


class SwiftS3Provider(S3Storage):
    """Provide isolated S3 buckets through Swift's Keystone EC2 credentials."""

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        keystone: KeystoneClient,
        cleanup_credentials: S3Credentials,
        region: str = "us-east-1",
        identity: str | None = None,
    ) -> None:
        self.keystone = keystone
        super().__init__(
            endpoint,
            namespace,
            SwiftCredentialProvider(keystone, cleanup_credentials),
            region=region,
            identity=identity,
        )
