# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

from resource_registry import ResourceRegistry
from resource_registry.protocols import ResourceHandle

from .provider import S3Bucket, S3Provider


@dataclass(frozen=True)
class S3SessionHandle:
    """Parent scope that lets shared S3 resources outlive their Juju consumers."""

    @property
    def resource_id(self) -> str:
        return "s3:session"

    @property
    def resource_type(self) -> str:
        return "s3:session"

    @property
    def path_segment(self) -> str:
        return "s3-session"


class S3ResourceRegistry:
    """Track one provider's resources under the session resource registry."""

    def __init__(
        self,
        provider: S3Provider,
        resource_registry: ResourceRegistry,
        cleanup_buckets: bool,
        parent: ResourceHandle | None = None,
    ) -> None:
        self._provider = provider
        self._resource_registry = resource_registry
        self._cleanup_buckets = cleanup_buckets
        resource_registry.register(handle=provider.handle, parent=parent)

    def new_bucket(self) -> S3Bucket:
        """Register a bucket before provisioning can create external state."""
        bucket = self._provider.new_bucket()
        self._resource_registry.register(
            handle=bucket.handle,
            destroyer=bucket.cleanup if self._cleanup_buckets else None,
            parent=self._provider.handle,
        )
        return bucket
