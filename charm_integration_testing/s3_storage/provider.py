# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class S3StorageHandle:
    namespace: str
    identity: str | None = None

    def __post_init__(self) -> None:
        if self.identity == "":
            raise ValueError("S3 storage identity must not be empty")

    @property
    def resource_id(self) -> str:
        if self.identity is None:
            return f"s3:storage:{self.path_segment}"
        return f"s3:storage:{self.identity}"

    @property
    def resource_type(self) -> str:
        return "s3:storage"

    @property
    def path_segment(self) -> str:
        identity = self.identity or self.namespace
        return f"s3-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class S3BucketHandle:
    namespace: str
    name: str

    @property
    def resource_id(self) -> str:
        return f"s3:bucket:{self.name}"

    @property
    def resource_type(self) -> str:
        return "s3:bucket"

    @property
    def path_segment(self) -> str:
        return f"s3-bucket-{hashlib.sha256(self.name.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class S3Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.access_key or not self.secret_key:
            raise ValueError("S3 access and secret keys must not be empty")


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
        """Provider identity used to namespace bucket handles."""

    @abstractmethod
    def new_bucket(self) -> S3Bucket:
        """Create a bucket resource before provisioning any external state."""


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
        """Delete this bucket using the provider's credentials."""
