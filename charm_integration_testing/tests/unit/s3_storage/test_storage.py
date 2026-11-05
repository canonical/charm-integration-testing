# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]
from mypy_boto3_s3 import S3Client
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import (
    S3Credentials,
    StaticS3Provider,
)

CREDS = S3Credentials("test-access", "test-secret")
NAMESPACE = "test-environment"
PREFIX = f"charmqa-s3-{hashlib.sha256(NAMESPACE.encode()).hexdigest()[:8]}-"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
OLD_BUCKET = f"{PREFIX}{int((NOW - timedelta(hours=5)).timestamp())}-{'a' * 16}"


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


def expected_tagging() -> dict[str, Any]:
    return {"TagSet": [{"Key": "charmqa-storage-owner", "Value": ANY}]}


def test_provision_sets_location_constraint_for_nondefault_region(client: S3Client) -> None:
    storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        CREDS,
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


def test_storage_owner_includes_application_identity() -> None:
    first_storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        S3Credentials("application-one", "secret-one"),
    )
    second_storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        S3Credentials("application-two", "secret-two"),
    )

    assert first_storage._bucket_owner != second_storage._bucket_owner


def test_retry_creation_sets_location_constraint_for_nondefault_region(client: S3Client) -> None:
    storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        CREDS,
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
        StaticS3Provider._delete_bucket(client, OLD_BUCKET)
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
            StaticS3Provider._delete_bucket(client, OLD_BUCKET)
        stubber.assert_no_pending_responses()


def test_registry_tears_down_each_bucket_and_continues_after_failure(client: S3Client) -> None:
    storage = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, client_factory=lambda _: client)
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(storage.handle)
    buckets = [storage.new_bucket(), storage.new_bucket()]
    for bucket in buckets:
        registry.register(bucket.handle, destroyer=bucket.cleanup, parent=storage.handle)

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


def test_bucket_cleanup_closes_client_even_when_deletion_fails(client: S3Client) -> None:
    storage = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, client_factory=lambda _: client)
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
        assert close.call_count == 2  # Provisioning and cleanup each own their client.
        stubber.assert_no_pending_responses()


def test_static_provider_uses_same_credentials_for_bucket_lifecycle_and_integrator(client: S3Client) -> None:
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        return client

    storage = StaticS3Provider(
        "https://s3.test",
        NAMESPACE,
        CREDS,
        client_factory=client_factory,
    )
    bucket = storage.new_bucket()
    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        # Existing shared-key buckets must remain discoverable by scheduled cleanup.
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {
                "Bucket": name,
                "Tagging": {"TagSet": [{"Key": "charmqa-storage-owner", "Value": "c2e84753f62c40532c8c0caa38f6cdab"}]},
            },
        )
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        assert bucket.provision() == (name, CREDS)
        empty_bucket(stubber, name)
        bucket.cleanup()

    assert client_credentials == [CREDS, CREDS]
    stubber.assert_no_pending_responses()


def test_bucket_cleanup_propagates_client_close_failure_without_retry(client: S3Client) -> None:
    storage = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, client_factory=lambda _: client)
    bucket = storage.new_bucket()
    failure = RuntimeError("Bucket client close failed")
    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": expected_tagging()})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()
        empty_bucket(stubber, name)
        with patch.object(client, "close", side_effect=failure) as close:
            with pytest.raises(RuntimeError) as error:
                bucket.cleanup()
        assert error.value is failure
        close.assert_called_once()
        stubber.assert_no_pending_responses()


def test_direct_provider_handles_distinguish_endpoints_and_credentials() -> None:
    providers = [
        StaticS3Provider("https://first-s3.test", NAMESPACE, CREDS),
        StaticS3Provider("https://second-s3.test", NAMESPACE, CREDS),
        StaticS3Provider("https://first-s3.test", NAMESPACE, S3Credentials("other-access", "other-secret")),
    ]

    assert len({provider.handle.resource_id for provider in providers}) == len(providers)


def test_direct_static_handles_include_secrets_without_changing_ownership() -> None:
    # GIVEN matching non-secret identities but different effective secrets
    original = StaticS3Provider("https://s3.test", NAMESPACE, CREDS)
    credentials = S3Credentials(CREDS.access_key, "different-secret")

    # WHEN constructed directly, THEN credential configurations differ but persistent ownership is unchanged
    other = StaticS3Provider("https://s3.test", NAMESPACE, credentials)
    assert original.handle.resource_id != other.handle.resource_id
    assert original._bucket_owner == other._bucket_owner
    assert credentials.secret_key not in other.handle.resource_id


def test_direct_static_handle_preserves_explicit_identity() -> None:
    provider = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, identity="explicit-provider")
    assert provider.handle.identity == "explicit-provider"


def test_direct_static_provider_rejects_empty_identity() -> None:
    # GIVEN an explicit identity that would collide across providers
    # WHEN constructing a provider, THEN reject it instead of registering an ambiguous handle
    with pytest.raises(ValueError, match="S3 storage identity must not be empty"):
        StaticS3Provider("https://s3.test", NAMESPACE, CREDS, identity="")


@pytest.mark.parametrize(("access_key", "secret_key"), [("", "secret"), ("access", ""), ("", "")])
def test_direct_credentials_reject_empty_keys(access_key: str, secret_key: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        S3Credentials(access_key, secret_key)
