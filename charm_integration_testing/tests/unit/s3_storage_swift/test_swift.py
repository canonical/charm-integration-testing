# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import boto3  # type: ignore[import-untyped]
import pytest
import requests
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]
from mypy_boto3_s3 import S3Client
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import S3Bucket, S3Credentials, S3Provider, S3Storage
from s3_storage_swift import (
    KeystoneClient,
    KeystoneResourceRegistry,
    SwiftCredentialProvider,
    SwiftCredentials,
    SwiftS3Provider,
)
from s3_storage_swift.configuration import provider_from_environment

CREDS = S3Credentials("test-access", "test-secret")
CLEANUP_CREDS = S3Credentials("cleanup-access", "cleanup-secret")
NAMESPACE = "test-environment"
PREFIX = f"charmqa-s3-{hashlib.sha256(NAMESPACE.encode()).hexdigest()[:8]}-"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
OLD_BUCKET = f"{PREFIX}{int((NOW - timedelta(hours=5)).timestamp())}-{'a' * 16}"


@pytest.fixture
def client() -> Iterator[S3Client]:
    result: S3Client = boto3.client(
        "s3", aws_access_key_id=CREDS.access_key, aws_secret_access_key=CREDS.secret_key, region_name="us-east-1"
    )
    yield result
    result.close()


@pytest.fixture
def registry() -> ResourceRegistry:
    return ResourceRegistry([], logging.getLogger(__name__))


def registered_bucket(storage: S3Storage, registry: ResourceRegistry) -> S3Bucket:
    registry.register(storage.handle)
    bucket = storage.new_bucket()
    registry.register(bucket.handle, destroyer=bucket.cleanup, parent=storage.handle)
    return bucket


def empty_bucket(stubber: Stubber, name: str = ANY) -> None:
    stubber.add_response("list_multipart_uploads", {"Uploads": []}, {"Bucket": name})
    stubber.add_response("list_object_versions", {"Versions": [], "DeleteMarkers": []}, {"Bucket": name})
    stubber.add_response("delete_bucket", {}, {"Bucket": name})


def expected_tagging(access_key: str | None = None) -> dict[str, Any]:
    tags = [{"Key": "charmqa-storage-owner", "Value": ANY}]
    if access_key is not None:
        tags.append({"Key": "charmqa-ec2-access", "Value": access_key})
    return {"TagSet": tags}


SWIFT = SwiftCredentials(
    "https://identity.test/v3",
    "user",
    "test-password",
    "users",
    "project",
    "projects",
    "https://storage.test/swift/v1/AUTH_project",
)


class KeystoneStub(KeystoneClient):
    def __init__(self) -> None:
        self.credentials = SWIFT
        self.created = 0
        self.deleted: list[str] = []
        self.delete_attempts: list[str] = []
        self.fail_delete: set[str] = set()

    def create_credentials(self, *, on_access_key: Callable[[str], None] | None = None) -> S3Credentials:
        self.created += 1
        credentials = S3Credentials(f"test-access-{self.created}", f"test-secret-{self.created}")
        if on_access_key is not None:
            on_access_key(credentials.access_key)
        return credentials

    def delete_credentials(self, access_key: str) -> None:
        self.delete_attempts.append(access_key)
        if access_key in self.fail_delete:
            raise RuntimeError("Credential revocation failed")
        self.deleted.append(access_key)


@pytest.mark.parametrize(
    "auth_url",
    ["https://identity.test", "https://identity.test/", "https://identity.test/v3/"],
    ids=["implicit-version", "implicit-version-slash", "explicit-version-slash"],
)
@pytest.mark.parametrize(
    "storage_url", [SWIFT.storage_url, f"{SWIFT.storage_url}/"], ids=["storage-canonical", "storage-trailing-slash"]
)
def test_swift_stale_cleanup_accepts_equivalent_urls(client: S3Client, auth_url: str, storage_url: str) -> None:
    # GIVEN a bucket tagged by the canonical provider and an equivalent cleanup configuration
    original_keystone = KeystoneStub()
    original = SwiftS3Provider("https://s3.test", NAMESPACE, original_keystone, CLEANUP_CREDS)
    keystone = KeystoneStub()
    keystone.credentials = replace(SWIFT, auth_url=auth_url, storage_url=storage_url)
    storage = SwiftS3Provider("https://s3.test/", NAMESPACE, keystone, CLEANUP_CREDS, client_factory=lambda _: client)

    # WHEN sweeping with the alternate URL, THEN the original tag is recognized and its key revoked
    with Stubber(client) as stubber:
        stubber.add_response("list_buckets", {"Buckets": [{"Name": OLD_BUCKET}]})
        stubber.add_response(
            "get_bucket_tagging",
            {
                "TagSet": [
                    {"Key": "charmqa-storage-owner", "Value": original._bucket_owner},
                    {"Key": "charmqa-ec2-access", "Value": "bucket-access"},
                ]
            },
            {"Bucket": OLD_BUCKET},
        )
        empty_bucket(stubber, OLD_BUCKET)
        assert storage.cleanup_stale(timedelta(hours=4), NOW) == [OLD_BUCKET]
        assert keystone.deleted == ["bucket-access"]
        stubber.assert_no_pending_responses()
    assert (
        SwiftCredentialProvider(keystone, CLEANUP_CREDS).ownership_identity
        == SwiftCredentialProvider(original_keystone, CLEANUP_CREDS).ownership_identity
    )


def response(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update(headers or {})
    return result


def test_provision_reuses_bucket_and_registers_key(client: S3Client, registry: ResourceRegistry) -> None:
    # GIVEN Keystone-backed storage and an empty S3 bucket
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        if credentials == CLEANUP_CREDS:
            assert keystone.deleted == ["test-access-1"]
        return client

    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=client_factory,
    )
    bucket_resource = registered_bucket(storage, registry)
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
        assert len(registry.children_of(bucket_resource.handle)) == 1
        registry.teardown_all()
        # THEN the bucket key is revoked while the dedicated cleanup key remains reusable
        assert bucket.startswith(PREFIX)
        assert keystone.created == 1
        assert keystone.deleted == ["test-access-1"]
        assert client_credentials == [creds, creds, CLEANUP_CREDS]
        stubber.assert_no_pending_responses()


def test_partial_provision_is_cleaned(client: S3Client, registry: ResourceRegistry) -> None:
    # GIVEN an S3 create call that reports failure after creating a bucket
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=lambda _: client,
    )
    bucket_resource = registered_bucket(storage, registry)
    with Stubber(client) as stubber:
        stubber.add_client_error("create_bucket", service_error_code="InternalError", expected_params={"Bucket": ANY})
        empty_bucket(stubber)
        # WHEN provisioning fails and session teardown runs
        with pytest.raises(Exception, match="InternalError"):
            bucket_resource.provision()
        registry.teardown_all()
        # THEN the bucket credential is revoked before bucket deletion using the cleanup identity
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_incomplete_bucket_provisioning_retries_tagging(client: S3Client, registry: ResourceRegistry) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=lambda _: client,
    )
    bucket_resource = registered_bucket(storage, registry)
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
        registry.teardown_all()
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_nonexistent_partial_bucket_revokes_key(client: S3Client, registry: ResourceRegistry) -> None:
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=lambda _: client,
    )
    bucket_resource = registered_bucket(storage, registry)
    with Stubber(client) as stubber:
        stubber.add_client_error("create_bucket", service_error_code="AccessDenied", expected_params={"Bucket": ANY})
        stubber.add_client_error(
            "list_multipart_uploads", service_error_code="NoSuchBucket", expected_params={"Bucket": ANY}
        )
        with pytest.raises(Exception, match="AccessDenied"):
            bucket_resource.provision()
        registry.teardown_all()
        assert keystone.deleted == ["test-access-1"]
        stubber.assert_no_pending_responses()


def test_client_initialization_failure_leaves_key_registered_for_teardown(registry: ResourceRegistry) -> None:
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def fail(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        raise RuntimeError("Client initialization failed")

    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=fail,
    )
    bucket_resource = registered_bucket(storage, registry)
    with pytest.raises(RuntimeError, match="Client initialization failed"):
        bucket_resource.provision()
    assert keystone.deleted == []
    assert len(registry.children_of(bucket_resource.handle)) == 1
    registry.teardown_all()
    assert keystone.created == 1
    assert client_credentials == [S3Credentials("test-access-1", "test-secret-1")]
    assert keystone.deleted == ["test-access-1"]
    assert keystone.delete_attempts == ["test-access-1"]


def test_client_initialization_retry_reuses_registered_credentials(
    client: S3Client, registry: ResourceRegistry
) -> None:
    keystone = KeystoneStub()
    client_credentials: list[S3Credentials] = []

    def client_factory(credentials: S3Credentials) -> S3Client:
        client_credentials.append(credentials)
        if len(client_credentials) == 1:
            raise RuntimeError("Client initialization failed")
        return client

    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=client_factory,
    )
    bucket_resource = registered_bucket(storage, registry)
    with Stubber(client) as stubber:
        with pytest.raises(RuntimeError, match="Client initialization failed"):
            bucket_resource.provision()

        stubber.add_response("create_bucket", {}, {"Bucket": ANY})
        stubber.add_response(
            "put_bucket_tagging",
            {},
            {"Bucket": ANY, "Tagging": expected_tagging("test-access-1")},
        )
        stubber.add_response("put_object", {}, {"Bucket": ANY, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": ANY})
        bucket_resource.provision()

        empty_bucket(stubber)
        registry.teardown_all()

    assert keystone.created == 1
    assert keystone.deleted == ["test-access-1"]
    assert client_credentials == [
        S3Credentials("test-access-1", "test-secret-1"),
        S3Credentials("test-access-1", "test-secret-1"),
        CLEANUP_CREDS,
    ]
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize("failure", ["credential-revocation", "bucket-deletion"])
def test_teardown_continues_after_resource_failure(client: S3Client, registry: ResourceRegistry, failure: str) -> None:
    # GIVEN a registered bucket and credential, one of which cannot be destroyed
    keystone = KeystoneStub()
    storage = S3Storage(
        "https://s3.test",
        NAMESPACE,
        SwiftCredentialProvider(keystone, CLEANUP_CREDS, KeystoneResourceRegistry(registry, True)),
        client_factory=lambda _: client,
    )
    bucket = registered_bucket(storage, registry)
    with Stubber(client) as stubber:
        name = bucket.handle.name
        stubber.add_response("create_bucket", {}, {"Bucket": name})
        stubber.add_response("put_bucket_tagging", {}, {"Bucket": name, "Tagging": ANY})
        stubber.add_response("put_object", {}, {"Bucket": name, "Key": "charmqa/", "Body": b""})
        stubber.add_response("head_bucket", {}, {"Bucket": name})
        bucket.provision()
        if failure == "credential-revocation":
            keystone.fail_delete.add("test-access-1")
            empty_bucket(stubber, name)
        else:
            stubber.add_client_error(
                "list_multipart_uploads", service_error_code="AccessDenied", expected_params={"Bucket": name}
            )

        # WHEN teardown runs, THEN both destroyers are attempted exactly once
        with pytest.warns(ResourceTeardownWarning):
            registry.teardown_all()
        assert keystone.delete_attempts == ["test-access-1"]
        assert keystone.deleted == ([] if failure == "credential-revocation" else ["test-access-1"])
        assert registry.registered_handles() == []
        stubber.assert_no_pending_responses()


def test_swift_provisioning_requires_resource_registration(registry: ResourceRegistry) -> None:
    # GIVEN a provider with a shared registry but no registered bucket
    keystone = KeystoneStub()
    storage = SwiftS3Provider(
        "https://s3.test", NAMESPACE, keystone, CLEANUP_CREDS, resources=KeystoneResourceRegistry(registry, True)
    )

    # WHEN provisioning is attempted directly, THEN credentials cannot escape tracking
    with pytest.raises(ValueError, match="Parent .* is not registered"):
        storage.new_bucket().provision()
    assert keystone.created == 0


def test_cleanup_only_swift_provider_cannot_issue_untracked_credentials() -> None:
    # GIVEN a provider loaded without a session resource registry for stale cleanup
    keystone = KeystoneStub()
    storage = SwiftS3Provider("https://s3.test", NAMESPACE, keystone, CLEANUP_CREDS)

    # WHEN provisioning is attempted, THEN it fails before issuing a credential
    with pytest.raises(RuntimeError, match="requires a KeystoneResourceRegistry"):
        storage.new_bucket().provision()
    assert keystone.created == 0


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


def test_stale_cleanup_is_scoped_to_swift_cleanup_identity(client: S3Client) -> None:
    with requests.Session() as session:
        storage = SwiftS3Provider(
            "https://s3.test",
            NAMESPACE,
            KeystoneClient(SWIFT, session),
            CLEANUP_CREDS,
            client_factory=lambda _: client,
        )
        other_storage = SwiftS3Provider(
            "https://s3.test",
            NAMESPACE,
            KeystoneClient(SWIFT, session),
            S3Credentials("other-cleanup", "other-secret"),
        )
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


@pytest.mark.parametrize("delete_status", [204, 404], ids=["deleted", "already-absent"])
def test_keystone_scopes_credentials_and_refreshes_for_cleanup(delete_status: int) -> None:
    # GIVEN a valid Keystone v3 identity response
    token = response(
        201, {"token": {"user": {"id": "user-id"}, "project": {"id": "project-id"}}}, {"X-Subject-Token": "token"}
    )
    credential = response(201, {"credential": {"access": CREDS.access_key, "secret": CREDS.secret_key}})
    with requests.Session() as session, patch.object(session, "post", side_effect=[token, credential, token]) as post:
        with patch.object(session, "delete", return_value=response(delete_status, {})) as delete:
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


@pytest.mark.parametrize(
    ("access_key", "path_segment"),
    [
        pytest.param("normal-key", "normal-key", id="ordinary-key"),
        pytest.param("key/other?query#fragment", "key%2Fother%3Fquery%23fragment", id="reserved-characters"),
        pytest.param("key%2Fother", "key%252Fother", id="encoded-separator"),
    ],
)
def test_keystone_deletion_encodes_access_key_path_segment(access_key: str, path_segment: str) -> None:
    # GIVEN a key read from bucket metadata and an authenticated Keystone identity
    token = response(
        201, {"token": {"user": {"id": "user-id"}, "project": {"id": "project-id"}}}, {"X-Subject-Token": "token"}
    )
    with (
        requests.Session() as session,
        patch.object(session, "post", return_value=token),
        patch.object(session, "delete", return_value=response(204, {})) as delete,
    ):
        # WHEN cleanup revokes the key, THEN it remains exactly one URL path segment
        KeystoneClient(SWIFT, session).delete_credentials(access_key)
        delete.assert_called_once_with(
            f"{SWIFT.auth_url}/users/user-id/credentials/OS-EC2/{path_segment}",
            headers={"X-Auth-Token": "token"},
            timeout=30,
        )


@pytest.mark.parametrize("access_key", ["", ".", ".."], ids=["empty", "current-directory", "parent-directory"])
def test_keystone_deletion_rejects_relative_or_empty_access_key(access_key: str) -> None:
    # GIVEN a tag that cannot identify a single credential endpoint
    with requests.Session() as session, patch.object(session, "request") as request:
        # WHEN deletion is requested, THEN URL normalization cannot redirect it
        with pytest.raises(ValueError, match="empty or a relative path segment"):
            KeystoneClient(SWIFT, session).delete_credentials(access_key)
        request.assert_not_called()


def test_environment_rejects_swift_config_without_storage_url() -> None:
    with requests.Session() as session, pytest.raises(ValueError, match="S3_OS_STORAGE_URL"):
        provider_from_environment({"S3_OS_AUTH_URL": SWIFT.auth_url, "S3_OS_PASSWORD": SWIFT.password}, session)


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
