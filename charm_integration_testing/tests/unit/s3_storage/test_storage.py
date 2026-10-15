# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import json
import logging
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import boto3  # type: ignore[import-untyped]
import pytest
import requests
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]
from mypy_boto3_s3 import S3Client
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import (
    KeystoneClient,
    S3BucketHandle,
    S3Credentials,
    S3Provider,
    S3Storage,
    StaticCredentialProvider,
    StaticS3Provider,
    SwiftCredentialProvider,
    SwiftCredentials,
    SwiftS3Provider,
)
from s3_storage.environment import provider_from_environment

CREDS = S3Credentials("test-access", "test-secret")
CLEANUP_CREDS = S3Credentials("cleanup-access", "cleanup-secret")
SWIFT = SwiftCredentials(
    "https://identity.test/v3",
    "user",
    "test-password",
    "users",
    "project",
    "projects",
    "https://storage.test/swift/v1/AUTH_project",
)
NAMESPACE = "test-environment"
PREFIX = f"charmqa-s3-{hashlib.sha256(NAMESPACE.encode()).hexdigest()[:8]}-"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
OLD_BUCKET = f"{PREFIX}{int((NOW - timedelta(hours=5)).timestamp())}-{'a' * 16}"


class KeystoneStub(KeystoneClient):
    def __init__(self) -> None:
        self.credentials = SWIFT
        self.created = 0
        self.deleted: list[str] = []
        self.delete_attempts: list[str] = []
        self.fail_delete: set[str] = set()
        self.fail_delete_attempts: dict[str, int] = {}

    def create_credentials(self) -> S3Credentials:
        self.created += 1
        return S3Credentials(f"test-access-{self.created}", f"test-secret-{self.created}")

    def delete_credentials(self, access_key: str) -> None:
        self.delete_attempts.append(access_key)
        failures_remaining = self.fail_delete_attempts.get(access_key, 0)
        if access_key in self.fail_delete or failures_remaining:
            if failures_remaining:
                self.fail_delete_attempts[access_key] = failures_remaining - 1
            raise RuntimeError("Credential revocation failed")
        self.deleted.append(access_key)


@pytest.fixture
def client() -> Iterator[S3Client]:
    client: S3Client = boto3.client(
        "s3", aws_access_key_id=CREDS.access_key, aws_secret_access_key=CREDS.secret_key, region_name="us-east-1"
    )
    yield client
    client.close()


def empty_bucket(stubber: Stubber, name: str = ANY) -> None:
    stubber.add_response("list_multipart_uploads", {"Uploads": []}, {"Bucket": name})
    stubber.add_response("list_object_versions", {"Versions": [], "DeleteMarkers": []}, {"Bucket": name})
    stubber.add_response("delete_bucket", {}, {"Bucket": name})


def expected_tagging(access_key: str | None = None) -> dict[str, Any]:
    tags = [{"Key": "charmqa-storage-owner", "Value": ANY}]
    if access_key is not None:
        tags.append({"Key": "charmqa-ec2-access", "Value": access_key})
    return {"TagSet": tags}


def test_provision_reuses_bucket_and_revokes_key(client: S3Client) -> None:
    # GIVEN Keystone-backed storage and an empty S3 bucket
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        return client

    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=client_factory
    )
    bucket_resource = storage.new_bucket()
    with Stubber(client) as stubber:
        stubber.add_response("create_bucket", {}, {"Bucket": ANY})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": ANY, "Tagging": expected_tagging("test-access-1")},
        )
        stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": ANY, "Tagging": expected_tagging("test-access-1")},
        )
        stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        empty_bucket(stubber)
        # WHEN the same integrator is deployed twice then torn down
        bucket, creds = bucket_resource.provision()
        assert bucket_resource.provision() == (bucket, creds)
        bucket_resource.cleanup()
        # THEN the bucket key is revoked while the dedicated cleanup key remains reusable
        assert bucket.startswith(PREFIX)
        assert keystone.created == 1
        assert keystone.deleted == ["test-access-1"]
        assert client_credentials == [creds, CLEANUP_CREDS]
        stubber.assert_no_pending_responses()


def test_provision_sets_location_constraint_for_nondefault_region(client: S3Client) -> None:
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        StaticCredentialProvider(CREDS),
        region="eu-west-1",
        client_factory=lambda _: client,
    )
    bucket = storage.new_bucket()
    create_params = {
        "Bucket": bucket.handle.name,
        "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"},
    }
    with Stubber(client) as stubber:
        stubber.add_response("create_bucket", {}, create_params)
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": bucket.handle.name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": bucket.handle.name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": bucket.handle.name})
        bucket.provision()
        empty_bucket(stubber, bucket.handle.name)
        bucket.cleanup()
        stubber.assert_no_pending_responses()


def test_generic_storage_owner_includes_application_identity() -> None:
    first_storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        StaticCredentialProvider(S3Credentials("application-one", "secret-one"), CLEANUP_CREDS),
    )
    second_storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        StaticCredentialProvider(S3Credentials("application-two", "secret-two"), CLEANUP_CREDS),
    )

    assert first_storage._bucket_owner != second_storage._bucket_owner


def test_retry_creation_sets_location_constraint_for_nondefault_region(client: S3Client) -> None:
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        StaticCredentialProvider(CREDS),
        region="eu-west-1",
        client_factory=lambda _: client,
    )
    bucket = storage.new_bucket()
    create_params = {
        "Bucket": bucket.handle.name,
        "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"},
    }
    with Stubber(client) as stubber:
        stubber.add_client_error("create_bucket", service_error_code="InternalError", expected_params=create_params)
        with pytest.raises(Exception, match="InternalError"):
            bucket.provision()

        stubber.add_client_error(
            "head_bucket",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": bucket.handle.name},
        )
        stubber.add_response("create_bucket", {}, create_params)
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": bucket.handle.name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": bucket.handle.name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": bucket.handle.name})
        bucket.provision()
        empty_bucket(stubber, bucket.handle.name)
        bucket.cleanup()
        stubber.assert_no_pending_responses()


def test_partial_provision_is_cleaned(client: S3Client) -> None:
    # GIVEN an S3 create call that reports failure after creating a bucket
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    bucket_resource = storage.new_bucket()
    with Stubber(client) as stubber:
        stubber.add_client_error("create_bucket", service_error_code="InternalError", expected_params={"Bucket": ANY})
        empty_bucket(stubber)
        # WHEN provisioning fails and session teardown runs
        with pytest.raises(Exception, match="InternalError"):
            bucket_resource.provision()
        bucket_resource.cleanup()
        # THEN the bucket credential is revoked before bucket deletion using the cleanup identity
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_incomplete_bucket_provisioning_retries_tagging(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    bucket_resource = storage.new_bucket()
    with Stubber(client) as stubber:
        stubber.add_response("create_bucket", {}, {"Bucket": ANY})
        stubber.add_client_error(
            "put_bucket_tagging", service_error_code="AccessDenied", expected_params={"Bucket": ANY, "Tagging": ANY}
        )
        with pytest.raises(Exception, match="AccessDenied"):
            bucket_resource.provision()

        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": ANY, "Tagging": expected_tagging("test-access-1")},
        )
        stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        assert bucket_resource.provision() == (
            bucket_resource.handle.name,
            S3Credentials("test-access-1", "test-secret-1"),
        )

        empty_bucket(stubber)
        bucket_resource.cleanup()
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_nonexistent_partial_bucket_revokes_key(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    bucket_resource = storage.new_bucket()
    with Stubber(client) as stubber:
        stubber.add_client_error("create_bucket", service_error_code="AccessDenied", expected_params={"Bucket": ANY})
        stubber.add_client_error(
            "list_multipart_uploads", service_error_code="NoSuchBucket", expected_params={"Bucket": ANY}
        )
        with pytest.raises(Exception, match="AccessDenied"):
            bucket_resource.provision()
        bucket_resource.cleanup()
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_client_initialization_failure_revokes_bucket_key() -> None:
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def fail(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        raise RuntimeError("Client initialization failed")

    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=fail
    )
    bucket_resource = storage.new_bucket()
    with pytest.raises(RuntimeError, match="Client initialization failed"):
        bucket_resource.provision()
    assert keystone.deleted == ["test-access-1"]
    bucket_resource.cleanup()
    assert keystone.created == 1
    assert client_credentials == [S3Credentials("test-access-1", "test-secret-1")]
    assert keystone.deleted == ["test-access-1"]
    assert keystone.delete_attempts == ["test-access-1"]


def test_client_initialization_retry_issues_fresh_bucket_key(client: S3Client) -> None:
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        if credentials.access_key == "test-access-1":
            raise RuntimeError("Client initialization failed")
        return client

    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=client_factory
    )
    bucket_resource = storage.new_bucket()
    with Stubber(client) as stubber:
        with pytest.raises(RuntimeError, match="Client initialization failed"):
            bucket_resource.provision()

        stubber.add_response("create_bucket", {}, {"Bucket": ANY})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": ANY, "Tagging": expected_tagging("test-access-2")},
        )
        stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        bucket_resource.provision()

        empty_bucket(stubber)
        bucket_resource.cleanup()

    assert keystone.created == 2
    assert keystone.deleted == ["test-access-1", "test-access-2"]
    assert client_credentials == [
        S3Credentials("test-access-1", "test-secret-1"),
        S3Credentials("test-access-2", "test-secret-2"),
        CLEANUP_CREDS,
    ]
    stubber.assert_no_pending_responses()


def test_client_and_revocation_failure_remains_recoverable() -> None:
    keystone = KeystoneStub()
    keystone.fail_delete.add("test-access-1")
    client_credentials: list[S3Credentials] = []

    def fail(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        raise RuntimeError("Client initialization failed")

    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=fail
    )
    bucket_resource = storage.new_bucket()
    with pytest.raises(RuntimeError, match="could not be revoked"):
        bucket_resource.provision()

    with pytest.raises(RuntimeError, match="Failed to clean 1 pending S3 resource"):
        storage.cleanup_pending_resources()

    # WHEN session teardown retries revocation after the initial cleanup failure
    keystone.fail_delete.clear()
    storage.cleanup_pending_resources()

    # THEN the issued key was retained until Keystone confirmed revocation
    assert keystone.created == 1
    assert keystone.delete_attempts == ["test-access-1", "test-access-1", "test-access-1"]
    assert keystone.deleted == ["test-access-1"]
    assert client_credentials == [S3Credentials("test-access-1", "test-secret-1")]


def test_client_initialization_retry_uses_fresh_credentials(client: S3Client) -> None:
    keystone = KeystoneStub()
    keystone.fail_delete_attempts["test-access-1"] = 1
    client_credentials: list[S3Credentials] = []
    stubber = Stubber(client)

    def create_client(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        if credentials.access_key == "test-access-1":
            raise RuntimeError("Client initialization failed")
        return client

    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS),
        client_factory=create_client,
    )
    bucket_resource = storage.new_bucket()
    with pytest.raises(RuntimeError, match="could not be revoked"):
        bucket_resource.provision()

    stubber.add_response("create_bucket", {}, {"Bucket": ANY})
    stubber.add_response(
        "put_bucket_tagging",
        {},
        {"Bucket": ANY, "Tagging": expected_tagging("test-access-2")},
    )
    stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
    stubber.add_response("head_bucket", {}, {"Bucket": ANY})
    empty_bucket(stubber)
    with stubber:
        _, credentials = bucket_resource.provision()
        storage.cleanup_pending_resources()
        bucket_resource.cleanup()

    assert credentials == S3Credentials("test-access-2", "test-secret-2")
    assert keystone.created == 2
    assert keystone.delete_attempts == ["test-access-1", "test-access-1", "test-access-2"]
    assert keystone.deleted == ["test-access-1", "test-access-2"]
    assert client_credentials == [
        S3Credentials("test-access-1", "test-secret-1"),
        S3Credentials("test-access-2", "test-secret-2"),
        CLEANUP_CREDS,
    ]
    stubber.assert_no_pending_responses()


def test_registry_retries_pending_credential_revocation() -> None:
    keystone = KeystoneStub()
    keystone.fail_delete_attempts["test-access-1"] = 1

    def fail(_: S3Credentials) -> S3Client:
        raise RuntimeError("Client initialization failed")

    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=fail
    )
    bucket = storage.new_bucket()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(bucket.handle, destroyer=storage.bucket_destroyer(bucket))

    with pytest.raises(RuntimeError, match="could not be revoked"):
        bucket.provision()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceTeardownWarning)
        registry.teardown_all()

    # The bucket destroyer retries the pending key after bucket teardown.
    assert keystone.delete_attempts == ["test-access-1"] * 2
    assert keystone.deleted == ["test-access-1"]
    assert not caught


def test_cleanup_removes_versions_markers_and_multipart(client: S3Client) -> None:
    # GIVEN a bucket with multiple pages of versions and a multipart upload
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_multipart_uploads", {"Uploads": [{"Key": "partial", "UploadId": "upload"}]}, {"Bucket": OLD_BUCKET}
        )
        stubber.add_response(
            "abort_multipart_upload", {}, {"Bucket": OLD_BUCKET, "Key": "partial", "UploadId": "upload"}
        )
        stubber.add_response(
            "list_object_versions",
            {
                "IsTruncated": True,
                "NextKeyMarker": "key",
                "NextVersionIdMarker": "v1",
                "Versions": [{"Key": "key", "VersionId": "v1"}],
                "DeleteMarkers": [{"Key": "deleted", "VersionId": "marker"}],
            },
            {"Bucket": OLD_BUCKET},
        )
        stubber.add_response(
            "delete_objects",
            {},
            {
                "Bucket": OLD_BUCKET,
                "Delete": {"Objects": [{"Key": "key", "VersionId": "v1"}, {"Key": "deleted", "VersionId": "marker"}]},
            },
        )
        stubber.add_response(
            "list_object_versions",
            {"IsTruncated": False, "Versions": [{"Key": "other", "VersionId": "v2"}]},
            {"Bucket": OLD_BUCKET, "KeyMarker": "key", "VersionIdMarker": "v1"},
        )
        stubber.add_response(
            "delete_objects", {}, {"Bucket": OLD_BUCKET, "Delete": {"Objects": [{"Key": "other", "VersionId": "v2"}]}}
        )
        stubber.add_response("delete_bucket", {}, {"Bucket": OLD_BUCKET})
        # WHEN the bucket is cleaned
        S3Storage._delete_bucket(client, OLD_BUCKET)
        # THEN all versions, markers and uploads were deleted before the bucket
        stubber.assert_no_pending_responses()


def test_cleanup_surfaces_per_object_errors(client: S3Client) -> None:
    with Stubber(client) as stubber:
        stubber.add_response("list_multipart_uploads", {}, {"Bucket": OLD_BUCKET})
        stubber.add_response(
            "list_object_versions", {"Versions": [{"Key": "key", "VersionId": "v1"}]}, {"Bucket": OLD_BUCKET}
        )
        stubber.add_response(
            "delete_objects",
            {"Errors": [{"Key": "key", "Code": "AccessDenied"}]},
            {"Bucket": OLD_BUCKET, "Delete": {"Objects": [{"Key": "key", "VersionId": "v1"}]}},
        )
        with pytest.raises(RuntimeError, match="S3 object deletion failed"):
            S3Storage._delete_bucket(client, OLD_BUCKET)
        stubber.assert_no_pending_responses()


def test_registry_tears_down_each_bucket_and_continues_after_failure(client: S3Client) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS), client_factory=lambda _: client)
    registry = ResourceRegistry([], logging.getLogger(__name__))
    buckets = [storage.new_bucket(), storage.new_bucket()]
    for bucket in buckets:
        registry.register(bucket.handle, destroyer=storage.bucket_destroyer(bucket))

    with Stubber(client) as stubber:
        for bucket in buckets:
            name = bucket.handle.name
            stubber.add_response("create_bucket", {}, {"Bucket": name})
            stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": expected_tagging()})
            stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
            stubber.add_response("head_bucket", {}, {"Bucket": name})
            bucket.provision()
        stubber.add_client_error(
            "list_multipart_uploads",
            service_error_code="AccessDenied",
            expected_params={"Bucket": buckets[0].handle.name},
        )
        empty_bucket(stubber, buckets[1].handle.name)
        with pytest.warns(ResourceTeardownWarning):
            registry.teardown_all()

        assert registry.registered_handles() == []
        stubber.assert_no_pending_responses()


def test_static_bucket_cleanup_keeps_client_open_for_retry(client: S3Client) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS), client_factory=lambda _: client)
    bucket = storage.new_bucket()
    with patch.object(client, "close", wraps=client.close) as close, Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()

        stubber.add_client_error(
            "list_multipart_uploads", service_error_code="AccessDenied", expected_params={"Bucket": name}
        )
        with pytest.raises(Exception, match="AccessDenied"):
            bucket.cleanup()
        close.assert_not_called()

        empty_bucket(stubber, name)
        bucket.cleanup()
        close.assert_called_once()
        stubber.assert_no_pending_responses()


def test_static_provider_uses_management_credentials_for_bucket_lifecycle(client: S3Client) -> None:
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        return client

    storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        CREDS,
        client_factory=client_factory,
        cleanup_credentials=CLEANUP_CREDS,
    )
    bucket = storage.new_bucket()
    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        assert bucket.provision() == (name, CREDS)
        empty_bucket(stubber, name)
        bucket.cleanup()

    assert client_credentials == [CLEANUP_CREDS]
    stubber.assert_no_pending_responses()


def test_cleanup_retries_bucket_when_credential_revocation_fails(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    bucket = storage.new_bucket()
    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": ANY})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()
        keystone.fail_delete.add("test-access-1")

        with pytest.raises(RuntimeError, match="Credential revocation failed"):
            bucket.cleanup()

        assert keystone.delete_attempts == ["test-access-1"]
        assert keystone.deleted == []
        empty_bucket(stubber, name)
        keystone.fail_delete.clear()
        bucket.cleanup()
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_cleanup_revokes_key_when_cleanup_client_initialization_fails(client: S3Client) -> None:
    keystone = KeystoneStub()
    cleanup_client: S3Client = boto3.client(
        "s3",
        aws_access_key_id=CLEANUP_CREDS.access_key,
        aws_secret_access_key=CLEANUP_CREDS.secret_key,
        region_name="us-east-1",
    )
    cleanup_client_attempts = 0

    def client_factory(credentials: S3Credentials) -> S3Client:
        nonlocal cleanup_client_attempts
        if credentials == CLEANUP_CREDS:
            cleanup_client_attempts += 1
            if cleanup_client_attempts == 1:
                raise RuntimeError("Cleanup client initialization failed")
            return cleanup_client
        return client

    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS),
        client_factory=client_factory,
    )
    bucket = storage.new_bucket()
    with (
        patch.object(client, "close", wraps=client.close) as close,
        Stubber(client) as bucket_stubber,
        Stubber(cleanup_client) as cleanup_stubber,
    ):
        name = bucket.handle.name
        bucket_stubber.add_response("create_bucket", {}, {"Bucket": name})
        bucket_stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": ANY})
        bucket_stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        bucket_stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()

        with pytest.raises(RuntimeError, match="Cleanup client initialization failed"):
            bucket.cleanup()

        assert keystone.deleted == ["test-access-1"]
        close.assert_called_once()

        empty_bucket(cleanup_stubber, name)
        bucket.cleanup()
        cleanup_stubber.assert_no_pending_responses()
    cleanup_client.close()


def test_stale_cleanup_is_scoped_and_revokes_owned_keys(client: S3Client) -> None:
    # GIVEN old/fresh harness buckets, another environment, malformed names and a log container
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    fresh = f"{PREFIX}{int((NOW - timedelta(minutes=240)).timestamp())}-{'b' * 16}"
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_buckets",
            {
                "Buckets": [
                    {"Name": name}
                    for name in [
                        OLD_BUCKET,
                        fresh,
                        "charmqa-s3-other-1234567890-aaaaaaaaaaaaaaaa",
                        "charm-qa",
                        f"{PREFIX}not-a-bucket",
                    ]
                ]
            },
            {},
        )
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": storage._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": "old-key"},
                ]
            },
            {"Bucket": OLD_BUCKET},
        )
        empty_bucket(stubber, OLD_BUCKET)
        # WHEN cleanup runs at the four-hour threshold
        assert storage.cleanup_stale(timedelta(minutes=240), NOW) == [OLD_BUCKET]
        # THEN only the old bucket key is revoked; the configured cleanup key remains reusable
        assert keystone.deleted == ["old-key"]
        assert keystone.created == 0
        stubber.assert_no_pending_responses()


def test_stale_cleanup_does_not_delete_bucket_owned_by_another_provider(client: S3Client) -> None:
    storage = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, client_factory=lambda _: client)
    other_storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        S3Credentials("other-access", "other-secret"),
        client_factory=lambda _: client,
    )
    other_bucket = f"{PREFIX}{int((NOW - timedelta(hours=6)).timestamp())}-{'c' * 16}"
    assert storage._bucket_owner != other_storage._bucket_owner
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}, {"Name": other_bucket}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "charmqa-storage-owner", "Value": storage._bucket_owner}]},
            {"Bucket": OLD_BUCKET},
        )
        empty_bucket(stubber, OLD_BUCKET)
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "charmqa-storage-owner", "Value": other_storage._bucket_owner}]},
            {"Bucket": other_bucket},
        )

        assert storage.cleanup_stale(timedelta(hours=4), NOW) == [OLD_BUCKET]
        stubber.assert_no_pending_responses()


def test_stale_cleanup_is_scoped_to_swift_cleanup_identity(client: S3Client) -> None:
    with requests.Session() as session:
        storage = SwiftS3Provider("https://s3.test", NAMESPACE, KeystoneClient(SWIFT, session), CLEANUP_CREDS)
        other_storage = SwiftS3Provider(
            "https://s3.test",
            NAMESPACE,
            KeystoneClient(SWIFT, session),
            S3Credentials("other-cleanup", "other-secret"),
        )
        storage._client_factory = lambda _: client

        assert storage._bucket_owner != other_storage._bucket_owner
        assert storage.handle.resource_id != other_storage.handle.resource_id

        other_bucket = f"{PREFIX}{int((NOW - timedelta(hours=6)).timestamp())}-{'d' * 16}"
        with Stubber(client) as stubber:
            stubber.add_response("list_buckets", {"Buckets": [{"Name": other_bucket}]}, {})
            stubber.add_response(
                "get_bucket_tagging",
                {
                    "TagSet": [
                        {"Key": "charmqa-storage-owner", "Value": other_storage._bucket_owner},
                        {"Key": "charmqa-ec2-access", "Value": "other-key"},
                    ]
                },
                {"Bucket": other_bucket},
            )

            assert storage.cleanup_stale(timedelta(hours=4), NOW) == []
            stubber.assert_no_pending_responses()


def test_stale_cleanup_retains_bucket_without_owner_tag(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_client_error(
            "get_bucket_tagging", service_error_code="NoSuchTagSet", expected_params={"Bucket": OLD_BUCKET}
        )

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert str(error.value.__cause__) == "Missing ownership tags; retaining bucket"
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


def test_stale_cleanup_retains_bucket_with_empty_owner_tag(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "charmqa-storage-owner", "Value": ""}]},
            {"Bucket": OLD_BUCKET},
        )

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert str(error.value.__cause__) == "Missing ownership tag; retaining bucket"
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


def test_stale_cleanup_retains_owned_bucket_without_credential_tag(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "charmqa-storage-owner", "Value": storage._bucket_owner}]},
            {"Bucket": OLD_BUCKET},
        )

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert str(error.value.__cause__) == "Missing EC2 access-key tag; retaining bucket"
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


def test_stale_cleanup_retains_owned_bucket_with_empty_credential_tag(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": storage._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": ""},
                ]
            },
            {"Bucket": OLD_BUCKET},
        )

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert str(error.value.__cause__) == "Missing EC2 access-key tag; retaining bucket"
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


def test_static_stale_cleanup_checks_ownership_tag(client: S3Client) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS), client_factory=lambda _: client)
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {"TagSet": [{"Key": "charmqa-storage-owner", "Value": storage._bucket_owner}]},
            {"Bucket": OLD_BUCKET},
        )
        empty_bucket(stubber, OLD_BUCKET)
        assert storage.cleanup_stale(timedelta(hours=4), NOW) == [OLD_BUCKET]
        stubber.assert_no_pending_responses()


def test_stale_cleanup_retains_tagged_bucket_when_key_revocation_fails(client: S3Client) -> None:
    keystone = KeystoneStub()
    keystone.fail_delete.add("old-key")
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": storage._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": "old-key"},
                ]
            },
            {"Bucket": OLD_BUCKET},
        )

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert str(error.value.__cause__) == "Credential revocation failed"
        assert keystone.delete_attempts == ["old-key"]
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("failure", ["credential-revocation", "bucket-deletion"])
def test_stale_cleanup_continues_after_bucket_failure(client: S3Client, failure: str) -> None:
    keystone = KeystoneStub()
    if failure == "credential-revocation":
        keystone.fail_delete.add("failed-key")
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    failed_bucket = OLD_BUCKET
    later_bucket = f"{PREFIX}{int((NOW - timedelta(hours=5)).timestamp())}-{'b' * 16}"
    with patch.object(client, "close", wraps=client.close) as close, Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": failed_bucket}, {"Name": later_bucket}]}, {})
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": storage._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": "failed-key"},
                ]
            },
            {"Bucket": failed_bucket},
        )
        if failure == "bucket-deletion":
            stubber.add_client_error(
                "list_multipart_uploads",
                service_error_code="AccessDenied",
                expected_params={"Bucket": failed_bucket},
            )
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": storage._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": "later-key"},
                ]
            },
            {"Bucket": later_bucket},
        )
        empty_bucket(stubber, later_bucket)

        with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
            storage.cleanup_stale(timedelta(hours=4), NOW)

        assert failed_bucket in str(error.value)
        assert keystone.delete_attempts == ["failed-key", "later-key"]
        assert keystone.deleted == (
            ["later-key"] if failure == "credential-revocation" else ["failed-key", "later-key"]
        )
        close.assert_called_once()
        stubber.assert_no_pending_responses()


def test_registry_retries_failed_static_bucket_deletion(client: S3Client) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS), client_factory=lambda _: client)
    registry = ResourceRegistry([], logging.getLogger(__name__))
    bucket = storage.new_bucket()
    registry.register(bucket.handle, destroyer=storage.bucket_destroyer(bucket))

    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()
        stubber.add_client_error(
            "list_multipart_uploads", service_error_code="AccessDenied", expected_params={"Bucket": name}
        )
        empty_bucket(stubber, name)

        registry.teardown_all()

        assert registry.registered_handles() == []
        stubber.assert_no_pending_responses()


def test_cleanup_stale_failure_does_not_create_temporary_key(client: S3Client) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test", NAMESPACE, SwiftCredentialProvider(keystone, CLEANUP_CREDS), client_factory=lambda _: client
    )
    with Stubber(client) as stubber:
        stubber.add_client_error("list_buckets", service_error_code="AccessDenied", expected_params={})
        with pytest.raises(Exception, match="AccessDenied"):
            storage.cleanup_stale(timedelta(hours=4), NOW)
        assert keystone.created == 0
        assert keystone.deleted == []
        stubber.assert_no_pending_responses()


def test_registry_stops_consumers_before_bucket() -> None:
    from juju import JujuControllerHandle

    # GIVEN bucket resources registered before their consumers are bootstrapped
    events: list[str] = []
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(S3BucketHandle(NAMESPACE, "bucket"), destroyer=lambda: events.append("bucket"))
    registry.register(JujuControllerHandle("target"), destroyer=lambda: events.append("target"))
    registry.register(JujuControllerHandle("neighbor"), destroyer=lambda: events.append("neighbor"))
    # WHEN a failed or successful session tears down
    registry.teardown_all()
    # THEN consumers stop before the independent bucket root is removed
    assert events == ["neighbor", "target", "bucket"]


def response(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update(headers or {})
    return result


def test_keystone_scopes_credentials_and_refreshes_for_cleanup() -> None:
    # GIVEN a valid Keystone v3 identity response
    token = response(
        201, {"token": {"user": {"id": "user-id"}, "project": {"id": "project-id"}}}, {"X-Subject-Token": "token"}
    )
    credential = response(201, {"credential": {"access": CREDS.access_key, "secret": CREDS.secret_key}})
    with requests.Session() as session, patch.object(session, "post", side_effect=[token, credential, token]) as post:
        with patch.object(session, "delete", return_value=response(204, {})) as delete:
            keystone = KeystoneClient(SWIFT, session)
            # WHEN a key is created and revoked
            assert keystone.create_credentials() == CREDS
            keystone.delete_credentials(CREDS.access_key)
            # THEN the key belongs to the scoped project and cleanup refreshes the token
            assert post.call_count == 3
            assert post.call_args_list[1].kwargs["json"] == {"tenant_id": "project-id"}
            assert (
                post.call_args_list[0].kwargs["json"]["auth"]["identity"]["password"]["user"]["password"]
                == SWIFT.password
            )
            delete.assert_called_once_with(
                f"{SWIFT.auth_url}/users/user-id/credentials/OS-EC2/{CREDS.access_key}",
                headers={"X-Auth-Token": "token"},
                timeout=30,
            )


def test_keystone_error_does_not_include_secrets() -> None:
    with (
        requests.Session() as session,
        patch.object(session, "post", return_value=response(403, {"secret": SWIFT.password})),
    ):
        keystone = KeystoneClient(SWIFT, session)
        with pytest.raises(RuntimeError, match="HTTP 403") as error:
            keystone.create_credentials()
        assert SWIFT.password not in str(error.value)
        assert SWIFT.password not in repr(SWIFT)
        assert CREDS.secret_key not in repr(CREDS)


@pytest.mark.parametrize("age", [timedelta(0), timedelta(minutes=-1)])
def test_cleanup_rejects_nonpositive_age(age: timedelta) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS))
    with pytest.raises(ValueError, match="positive"):
        storage.cleanup_stale(age)


def test_environment_ignores_cloud_auth_without_storage_url() -> None:
    with requests.Session() as session:
        assert (
            provider_from_environment({"S3_OS_AUTH_URL": SWIFT.auth_url, "S3_OS_PASSWORD": SWIFT.password}, session)
            is None
        )


def test_environment_constructs_swift_provider() -> None:
    environ = {
        "S3_OS_AUTH_URL": SWIFT.auth_url,
        "S3_OS_USERNAME": SWIFT.username,
        "S3_OS_PASSWORD": SWIFT.password,
        "S3_OS_USER_DOMAIN_NAME": SWIFT.user_domain_name,
        "S3_OS_PROJECT_NAME": SWIFT.project_name,
        "S3_OS_PROJECT_DOMAIN_NAME": SWIFT.project_domain_name,
        "S3_OS_STORAGE_URL": SWIFT.storage_url,
        "S3_CLEANUP_ACCESS_KEY": CLEANUP_CREDS.access_key,
        "S3_CLEANUP_SECRET_KEY": CLEANUP_CREDS.secret_key,
    }
    with requests.Session() as session:
        provider = provider_from_environment(environ, session)
    assert provider is not None
    assert isinstance(provider, SwiftS3Provider)
    assert isinstance(provider, S3Provider)
    assert isinstance(provider.keystone, KeystoneClient)
    assert provider.endpoint == "https://storage.test"


def test_environment_reports_partial_credentials_without_values() -> None:
    with requests.Session() as session:
        with pytest.raises(ValueError, match="S3_OS_USERNAME") as error:
            provider_from_environment(
                {"S3_OS_STORAGE_URL": SWIFT.storage_url, "S3_OS_PASSWORD": SWIFT.password}, session
            )
        assert SWIFT.password not in str(error.value)
        with pytest.raises(ValueError, match="S3_SECRET_KEY"):
            provider_from_environment({"S3_ENDPOINT": "https://s3.test", "S3_ACCESS_KEY": CREDS.access_key}, session)
        with pytest.raises(ValueError, match="S3_CLEANUP_ACCESS_KEY"):
            provider_from_environment(
                {
                    "S3_OS_AUTH_URL": SWIFT.auth_url,
                    "S3_OS_USERNAME": SWIFT.username,
                    "S3_OS_PASSWORD": SWIFT.password,
                    "S3_OS_USER_DOMAIN_NAME": SWIFT.user_domain_name,
                    "S3_OS_PROJECT_NAME": SWIFT.project_name,
                    "S3_OS_PROJECT_DOMAIN_NAME": SWIFT.project_domain_name,
                    "S3_OS_STORAGE_URL": SWIFT.storage_url,
                },
                session,
            )


def test_environment_reports_prefixed_missing_credentials() -> None:
    prefix = "TARGET_"
    swift_environment = {
        f"{prefix}S3_OS_AUTH_URL": SWIFT.auth_url,
        f"{prefix}S3_OS_USERNAME": SWIFT.username,
        f"{prefix}S3_OS_PASSWORD": SWIFT.password,
        f"{prefix}S3_OS_USER_DOMAIN_NAME": SWIFT.user_domain_name,
        f"{prefix}S3_OS_PROJECT_NAME": SWIFT.project_name,
        f"{prefix}S3_OS_PROJECT_DOMAIN_NAME": SWIFT.project_domain_name,
        f"{prefix}S3_OS_STORAGE_URL": SWIFT.storage_url,
    }
    static_environment = {
        f"{prefix}S3_ENDPOINT": "https://s3.test",
        f"{prefix}S3_ACCESS_KEY": CREDS.access_key,
        f"{prefix}S3_SECRET_KEY": CREDS.secret_key,
    }

    with requests.Session() as session:
        with pytest.raises(ValueError, match="TARGET_S3_SECRET_KEY"):
            provider_from_environment(
                {f"{prefix}S3_ENDPOINT": "https://s3.test", f"{prefix}S3_ACCESS_KEY": CREDS.access_key},
                session,
                prefix=prefix,
            )
        with pytest.raises(ValueError, match="TARGET_S3_OS_AUTH_URL"):
            provider_from_environment(
                {f"{prefix}S3_OS_STORAGE_URL": SWIFT.storage_url},
                session,
                prefix=prefix,
            )
        with pytest.raises(ValueError, match="TARGET_S3_CLEANUP_SECRET_KEY"):
            provider_from_environment(
                {
                    **swift_environment,
                    f"{prefix}S3_CLEANUP_ACCESS_KEY": CLEANUP_CREDS.access_key,
                },
                session,
                prefix=prefix,
            )
        with pytest.raises(ValueError, match="TARGET_S3_CLEANUP_SECRET_KEY"):
            provider_from_environment(
                {
                    **static_environment,
                    f"{prefix}S3_CLEANUP_ACCESS_KEY": CLEANUP_CREDS.access_key,
                },
                session,
                prefix=prefix,
            )


def test_non_rgw_swift_url_needs_explicit_endpoint() -> None:
    swift = SwiftCredentials(
        SWIFT.auth_url,
        SWIFT.username,
        SWIFT.password,
        SWIFT.user_domain_name,
        SWIFT.project_name,
        SWIFT.project_domain_name,
        "https://swift.test/v1/AUTH_project",
    )
    with pytest.raises(ValueError, match="S3_ENDPOINT"):
        swift.s3_endpoint()


def test_static_environment_constructs_provider() -> None:
    # GIVEN the credentials produced by local sandbox setup
    environ = {
        "S3_ENDPOINT": "http://sandbox.test:8333",
        "S3_ACCESS_KEY": CREDS.access_key,
        "S3_SECRET_KEY": CREDS.secret_key,
        "S3_REGION": "local-region",
        "S3_BUCKET_NAMESPACE": NAMESPACE,
    }
    # WHEN fixture wiring reads the environment
    with requests.Session() as session:
        provider = provider_from_environment(environ, session)
    # THEN static credentials are passed through without Keystone authentication
    assert isinstance(provider, StaticS3Provider)
    assert provider.endpoint == environ["S3_ENDPOINT"]
    assert provider.region == "local-region"
    assert provider.handle.namespace == NAMESPACE


def test_static_environment_accepts_separate_management_credentials() -> None:
    environ = {
        "S3_ENDPOINT": "http://sandbox.test:8333",
        "S3_ACCESS_KEY": CREDS.access_key,
        "S3_SECRET_KEY": CREDS.secret_key,
        "S3_CLEANUP_ACCESS_KEY": CLEANUP_CREDS.access_key,
        "S3_CLEANUP_SECRET_KEY": CLEANUP_CREDS.secret_key,
    }

    with requests.Session() as session:
        provider = provider_from_environment(environ, session)

    assert isinstance(provider, StaticS3Provider)
    assert provider._credential_provider.issue() == CREDS
    assert provider._credential_provider.cleanup_credentials == CLEANUP_CREDS


def test_environment_provider_selects_credentials_by_prefix() -> None:
    environ = {
        "TARGET_S3_ENDPOINT": "https://target-s3.test",
        "TARGET_S3_ACCESS_KEY": "target-access",
        "TARGET_S3_SECRET_KEY": "target-secret",
        "TARGET_S3_BUCKET_NAMESPACE": "target-environment",
        "NEIGHBOR_S3_ENDPOINT": "https://neighbor-s3.test",
        "NEIGHBOR_S3_ACCESS_KEY": "neighbor-access",
        "NEIGHBOR_S3_SECRET_KEY": "neighbor-secret",
        "NEIGHBOR_S3_BUCKET_NAMESPACE": "neighbor-environment",
    }

    with requests.Session() as session:
        target = provider_from_environment(environ, session, prefix="TARGET_")
        neighbor = provider_from_environment(environ, session, prefix="NEIGHBOR_")

    assert isinstance(target, StaticS3Provider)
    assert target.endpoint == "https://target-s3.test"
    assert target.handle.namespace == "target-environment"
    assert isinstance(neighbor, StaticS3Provider)
    assert neighbor.endpoint == "https://neighbor-s3.test"
    assert neighbor.handle.namespace == "neighbor-environment"


def test_environment_provider_identity_distinguishes_storage_credentials() -> None:
    shared_namespace = "shared-environment"
    target_environment = {
        "S3_ENDPOINT": "https://target-s3.test",
        "S3_ACCESS_KEY": "target-access",
        "S3_SECRET_KEY": "target-secret",
        "S3_BUCKET_NAMESPACE": shared_namespace,
    }
    neighbor_environment = {
        "S3_ENDPOINT": "https://neighbor-s3.test",
        "S3_ACCESS_KEY": "neighbor-access",
        "S3_SECRET_KEY": "neighbor-secret",
        "S3_BUCKET_NAMESPACE": shared_namespace,
    }

    with requests.Session() as session:
        target = provider_from_environment(target_environment, session)
        neighbor = provider_from_environment(neighbor_environment, session)
        same_target = provider_from_environment(target_environment, session)

    assert isinstance(target, StaticS3Provider)
    assert isinstance(neighbor, StaticS3Provider)
    assert same_target is not None
    assert target.handle.namespace == neighbor.handle.namespace == shared_namespace
    assert target.handle.resource_id != neighbor.handle.resource_id
    assert target.handle.resource_id == same_target.handle.resource_id
    assert "target-secret" not in target.handle.resource_id


def test_environment_provider_identity_distinguishes_cleanup_credentials() -> None:
    common_credentials = {
        "S3_ENDPOINT": "https://s3.test",
        "S3_ACCESS_KEY": "application-access",
        "S3_SECRET_KEY": "application-secret",
        "S3_BUCKET_NAMESPACE": "shared-environment",
    }
    target_environment = {
        **common_credentials,
        "S3_CLEANUP_ACCESS_KEY": "target-cleanup-access",
        "S3_CLEANUP_SECRET_KEY": "target-cleanup-secret",
    }
    neighbor_environment = {
        **common_credentials,
        "S3_CLEANUP_ACCESS_KEY": "neighbor-cleanup-access",
        "S3_CLEANUP_SECRET_KEY": "neighbor-cleanup-secret",
    }

    with requests.Session() as session:
        target = provider_from_environment(target_environment, session)
        neighbor = provider_from_environment(neighbor_environment, session)

    assert isinstance(target, StaticS3Provider)
    assert isinstance(neighbor, StaticS3Provider)
    assert target.handle.resource_id != neighbor.handle.resource_id
    assert target._bucket_owner != neighbor._bucket_owner
    assert "target-cleanup-secret" not in target.handle.resource_id


def test_direct_provider_handles_distinguish_endpoints_and_credentials() -> None:
    providers = [
        StaticS3Provider("https://first-s3.test", NAMESPACE, CREDS),
        StaticS3Provider("https://second-s3.test", NAMESPACE, CREDS),
        StaticS3Provider("https://first-s3.test", NAMESPACE, S3Credentials("other-access", "other-secret")),
    ]

    assert len({provider.handle.resource_id for provider in providers}) == len(providers)
