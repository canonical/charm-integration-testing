# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from resource_registry import ResourceRegistry

from .provider import S3Bucket, S3Provider


class S3ResourceRegistry:
    """Track one provider's resources under the session resource registry."""

    def __init__(
        self,
        provider: S3Provider,
        resource_registry: ResourceRegistry,
        cleanup_buckets: bool,
    ) -> None:
        self._provider = provider
        self._resource_registry = resource_registry
        self._cleanup_buckets = cleanup_buckets
        resource_registry.register(
            handle=provider.handle,
            destroyer=provider.cleanup_pending_resources if cleanup_buckets else None,
        )

    def new_bucket(self) -> S3Bucket:
        """Register a bucket before provisioning; retry provider cleanup after all children."""
        bucket = self._provider.new_bucket()
        self._resource_registry.register(
            handle=bucket.handle,
            destroyer=self._provider.bucket_destroyer(bucket) if self._cleanup_buckets else None,
            parent=self._provider.handle,
        )
        return bucket
