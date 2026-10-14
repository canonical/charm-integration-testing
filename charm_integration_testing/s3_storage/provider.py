# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import S3BucketHandle, S3StorageHandle


@dataclass(frozen=True)
class S3Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


class S3Provider(ABC):
    """Create S3 bucket resources for s3-integrator applications."""

    @property
    @abstractmethod
    def endpoint(self) -> str:
        """S3-compatible endpoint configured on the integrator."""

    @property
    @abstractmethod
    def region(self) -> str:
        """S3 region configured on the integrator."""

    @property
    @abstractmethod
    def handle(self) -> S3StorageHandle:
        """Parent resource-registry identity for this provider's buckets."""

    @abstractmethod
    def new_bucket(self) -> S3Bucket:
        """Create a bucket resource before provisioning any external state."""

    @abstractmethod
    def cleanup_stale(self, age: timedelta, now: datetime | None = None) -> list[str]:
        """Remove old buckets owned by this provider's namespace."""

    def cleanup_pending_credentials(self) -> None:
        """Retry revocation of provider-owned credentials not attached to a bucket."""


class S3Bucket(ABC):
    """One bucket's provisioning state and cleanup operation."""

    @property
    @abstractmethod
    def handle(self) -> S3BucketHandle:
        """Resource-registry identity for this bucket."""

    @abstractmethod
    def provision(self) -> tuple[str, S3Credentials]:
        """Create and verify this bucket, returning its credentials."""

    @abstractmethod
    def cleanup(self) -> None:
        """Revoke bucket credentials and delete this bucket."""


class CredentialProvider(ABC):
    """Strategy for the credentials an S3 backend hands to buckets and to cleanup."""

    # Per-bucket credentials are revoked with their bucket and recorded in a bucket tag for stale sweeps.
    per_bucket_credentials: bool

    @property
    @abstractmethod
    def cleanup_credentials(self) -> S3Credentials:
        """Independent identity used to delete buckets after bucket credentials are revoked."""

    @abstractmethod
    def issue(self) -> S3Credentials:
        """Return credentials for a new bucket."""

    @abstractmethod
    def revoke(self, access_key: str) -> None:
        """Revoke credentials returned by issue(); must be idempotent."""


class StaticCredentialProvider(CredentialProvider):
    """Share one externally managed identity across buckets; nothing to issue or revoke."""

    per_bucket_credentials = False

    def __init__(self, credentials: S3Credentials) -> None:
        self.credentials = credentials

    @property
    def cleanup_credentials(self) -> S3Credentials:
        return self.credentials

    def issue(self) -> S3Credentials:
        return self.credentials

    def revoke(self, access_key: str) -> None:
        return None
