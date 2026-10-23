# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import pytest
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import S3Bucket, S3BucketHandle, S3Credentials, S3ResourceRegistry, StaticS3Provider


class BucketStub(S3Bucket):
    def __init__(self, name: str, events: list[str]) -> None:
        self._handle = S3BucketHandle("test", name)
        self.events = events
        self.fail_cleanup = False

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
    def __init__(self, name: str, events: list[str]) -> None:
        super().__init__("http://s3.test", name, S3Credentials("access", "secret"))
        self.events = events
        self.buckets: list[BucketStub] = []

    def new_bucket(self) -> S3Bucket:
        bucket = BucketStub(f"{self.handle.namespace}-{len(self.buckets)}", self.events)
        self.buckets.append(bucket)
        return bucket


def test_session_teardown_attempts_each_bucket_once_and_continues_after_failure() -> None:
    # GIVEN a provider scope whose first child fails cleanup
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    scope = S3ResourceRegistry(provider, registry, cleanup_buckets=True)
    first, second = scope.new_bucket(), scope.new_bucket()
    provider.buckets[0].fail_cleanup = True

    # WHEN teardown runs, THEN failure is reported without retrying or skipping siblings
    with pytest.warns(ResourceTeardownWarning, match="Cleanup failed for provider-0"):
        registry.teardown_all()
    assert events == [first.handle.name, second.handle.name]
    assert registry.registered_handles() == []


def test_failed_bucket_cleanup_does_not_skip_other_provider_scopes() -> None:
    # GIVEN independent provider scopes, with a failing bucket in the later scope
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    first_provider = ProviderStub("first", events)
    second_provider = ProviderStub("second", events)
    first = S3ResourceRegistry(first_provider, registry, cleanup_buckets=True).new_bucket()
    second = S3ResourceRegistry(second_provider, registry, cleanup_buckets=True).new_bucket()
    second_provider.buckets[0].fail_cleanup = True

    # WHEN teardown runs, THEN both scopes are processed
    with pytest.warns(ResourceTeardownWarning, match="Cleanup failed for second-0"):
        registry.teardown_all()
    assert events == [second.handle.name, first.handle.name]
    assert registry.registered_handles() == []


def test_bucket_is_registered_before_provisioning_can_fail() -> None:
    # GIVEN a provider-bound resource scope
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    bucket = S3ResourceRegistry(provider, registry, cleanup_buckets=True).new_bucket()

    # WHEN provisioning fails after mutation, THEN the session still owns cleanup
    with pytest.raises(RuntimeError, match="Provisioning failed after mutation"):
        bucket.provision()
    assert registry.children_of(provider.handle) == [bucket.handle]
    registry.teardown_all()
    assert events == [bucket.handle.name]


def test_resumed_runs_preserve_buckets() -> None:
    # GIVEN a resumed run that does not own storage cleanup
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = ProviderStub("provider", events)
    bucket = S3ResourceRegistry(provider, registry, cleanup_buckets=False).new_bucket()
    assert registry.children_of(provider.handle) == [bucket.handle]

    # WHEN teardown runs, THEN the bucket is preserved
    registry.teardown_all()
    assert events == []
    assert registry.registered_handles() == []
