# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import S3BucketHandle, S3StorageHandle


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
