# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from collections.abc import Callable

import pytest
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import S3Bucket, S3BucketHandle, S3Credentials, S3ResourceRegistry, StaticS3Provider


class BucketStub(S3Bucket):
    def __init__(self, name: str, events: list[str], fail_cleanup: bool = False) -> None:
        self._handle = S3BucketHandle("test", name)
        self.events = events
        self.fail_cleanup = fail_cleanup

    @property
    def handle(self) -> S3BucketHandle:
        return self._handle

    def provision(self) -> tuple[str, S3Credentials]:
        raise RuntimeError("Provisioning failed after mutation")

    def cleanup(self) -> None:
        self.events.append(self.handle.name)
        if self.fail_cleanup:
            raise RuntimeError(f"Cleanup failed for {self.handle.name}")


class ProviderStub(StaticS3Provider):
    def __init__(self, name: str, events: list[str], fail_cleanup: bool = False) -> None:
        super().__init__("http://s3.test", name, S3Credentials("access", "secret"))
        self.events = events
        self.fail_cleanup = fail_cleanup
        self.pending_cleanup_failures = 0
        self.buckets: list[BucketStub] = []

    def new_bucket(self) -> S3Bucket:
        bucket = BucketStub(f"{self.handle.namespace}-{len(self.buckets)}", self.events)
        self.buckets.append(bucket)
        return bucket

    def bucket_destroyer(self, bucket: S3Bucket) -> Callable[[], None]:
        return bucket.cleanup

    def cleanup_pending_resources(self) -> None:
        self.events.append(f"{self.handle.namespace}-pending")
        if self.fail_cleanup or self.pending_cleanup_failures:
            if not self.fail_cleanup:
                self.pending_cleanup_failures -= 1
            raise RuntimeError(f"Pending cleanup failed for {self.handle.namespace}")


def test_session_teardown_destroys_buckets_before_provider_pending_cleanup() -> None:
    # GIVEN a provider scope whose children fail cleanup independently
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    scope = S3ResourceRegistry(provider, registry, cleanup_buckets=True)
    first, second = scope.new_bucket(), scope.new_bucket()
    provider.buckets[0].fail_cleanup = True

    # WHEN session teardown runs, THEN all children run before provider cleanup
    with pytest.warns(ResourceTeardownWarning, match="Cleanup failed for provider-0"):
        registry.teardown_all()
    assert events == [first.handle.name, second.handle.name, "provider-pending"]
    assert registry.registered_handles() == []


def test_failed_provider_cleanup_does_not_skip_other_provider_scopes() -> None:
    # GIVEN two independent provider scopes, the later one failing final cleanup
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    first = S3ResourceRegistry(ProviderStub("first", events), registry, cleanup_buckets=True)
    second = S3ResourceRegistry(ProviderStub("second", events, fail_cleanup=True), registry, cleanup_buckets=True)
    first.new_bucket()
    second.new_bucket()

    # WHEN session teardown runs, THEN it reports the error and processes both scopes
    with pytest.warns(ResourceTeardownWarning, match="Pending cleanup failed for second"):
        registry.teardown_all()
    assert events == ["second-0", "second-pending", "second-pending", "first-0", "first-pending"]
    assert registry.registered_handles() == []


def test_provider_cleanup_is_retried_after_a_transient_failure() -> None:
    # GIVEN a provider whose first final cleanup attempt fails
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    provider.pending_cleanup_failures = 1
    S3ResourceRegistry(provider, registry, cleanup_buckets=True)

    # WHEN the provider resource is torn down, THEN the final retry completes cleanup
    registry.teardown_all()

    assert events == ["provider-pending", "provider-pending"]
    assert registry.registered_handles() == []


def test_bucket_is_registered_before_provisioning_can_fail() -> None:
    # GIVEN a provider-bound resource scope
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    scope = S3ResourceRegistry(provider, registry, cleanup_buckets=True)
    bucket = scope.new_bucket()

    # WHEN provisioning fails after mutation, THEN the session can still destroy the resource
    with pytest.raises(RuntimeError, match="Provisioning failed after mutation"):
        bucket.provision()
    assert registry.children_of(provider.handle) == [bucket.handle]
    registry.teardown_all()
    assert events == ["provider-0", "provider-pending"]


def test_resumed_runs_preserve_buckets_and_provider_pending_resources() -> None:
    # GIVEN a resumed run that does not own storage cleanup
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    scope = S3ResourceRegistry(provider, registry, cleanup_buckets=False)
    bucket = scope.new_bucket()
    assert registry.children_of(provider.handle) == [bucket.handle]

    # WHEN session teardown runs, THEN neither buckets nor provider retries are invoked
    registry.teardown_all()
    assert events == []
    assert registry.registered_handles() == []
