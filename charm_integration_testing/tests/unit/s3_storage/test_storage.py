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
    S3Storage,
    StaticCredentialProvider,
    StaticS3Provider,
)
from s3_storage.environment import provider_from_environment

CREDS = S3Credentials("test-access", "test-secret")
CLEANUP_CREDS = S3Credentials("cleanup-access", "cleanup-secret")
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
        assert close.call_count == 2  # Provisioning and cleanup each own their client.
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

    assert client_credentials == [CLEANUP_CREDS, CLEANUP_CREDS]
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


@pytest.mark.parametrize(
    "failure_stage", ["bucket", "listing", "none"], ids=["bucket-error", "listing-error", "close-only"]
)
def test_stale_cleanup_reports_sweep_and_client_close_failures(client: S3Client, failure_stage: str) -> None:
    # GIVEN a stale sweep whose client close fails, optionally after an S3 error
    storage = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, client_factory=lambda _: client)
    close_failure = RuntimeError("Close failed")
    with Stubber(client) as stubber:
        if failure_stage == "listing":
            stubber.add_client_error("list_buckets", service_error_code="AccessDenied", expected_params={})
        else:
            buckets = [{"Name": OLD_BUCKET}] if failure_stage == "bucket" else []
            stubber.add_response("list_buckets", {"Buckets": buckets}, {})
            if failure_stage == "bucket":
                stubber.add_client_error(
                    "get_bucket_tagging", service_error_code="AccessDenied", expected_params={"Bucket": OLD_BUCKET}
                )

        # WHEN cleanup closes its client, THEN both diagnostics survive and the first failure remains the cause
        with patch.object(client, "close", side_effect=close_failure) as close:
            with pytest.raises(RuntimeError, match="Failed to clean stale S3 resources") as error:
                storage.cleanup_stale(timedelta(hours=4), NOW)
        assert "client close: RuntimeError" in str(error.value)
        if failure_stage == "none":
            assert error.value.__cause__ is close_failure
        else:
            assert error.value.__cause__ is not None
            assert "AccessDenied" in str(error.value.__cause__)
            expected_context = (
                OLD_BUCKET if failure_stage == "bucket" else f"sweep: {type(error.value.__cause__).__name__}"
            )
            assert expected_context in str(error.value)
        close.assert_called_once()
        stubber.assert_no_pending_responses()


def test_bucket_cleanup_propagates_client_close_failure_without_retry(client: S3Client) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS), client_factory=lambda _: client)
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


@pytest.mark.parametrize("age", [timedelta(0), timedelta(minutes=-1)])
def test_cleanup_rejects_nonpositive_age(age: timedelta) -> None:
    storage = S3Storage("https://s3.test", NAMESPACE, StaticCredentialProvider(CREDS))
    with pytest.raises(ValueError, match="positive"):
        storage.cleanup_stale(age)


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
    provider = provider_from_environment(environ)
    # THEN static credentials are passed through to the provider
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

    provider = provider_from_environment(environ)

    assert isinstance(provider, StaticS3Provider)
    assert provider._credential_provider.issue(provider.new_bucket().handle) == CREDS
    assert provider._credential_provider.cleanup_credentials == CLEANUP_CREDS


def test_static_environment_rejects_partial_credentials() -> None:
    with pytest.raises(ValueError, match="S3_SECRET_KEY"):
        provider_from_environment({"S3_ENDPOINT": "https://s3.test", "S3_ACCESS_KEY": CREDS.access_key})

    with pytest.raises(ValueError, match="S3_CLEANUP_ACCESS_KEY"):
        provider_from_environment(
            {
                "S3_ENDPOINT": "https://s3.test",
                "S3_ACCESS_KEY": CREDS.access_key,
                "S3_SECRET_KEY": CREDS.secret_key,
                "S3_CLEANUP_SECRET_KEY": CLEANUP_CREDS.secret_key,
            }
        )


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

    target = provider_from_environment(environ, prefix="TARGET_")
    neighbor = provider_from_environment(environ, prefix="NEIGHBOR_")

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

    target = provider_from_environment(target_environment)
    neighbor = provider_from_environment(neighbor_environment)
    same_target = provider_from_environment(target_environment)

    assert isinstance(target, StaticS3Provider)
    assert isinstance(neighbor, StaticS3Provider)
    assert same_target is not None
    assert target.handle.namespace == neighbor.handle.namespace == shared_namespace
    assert target.handle.resource_id != neighbor.handle.resource_id
    assert target.handle.resource_id == same_target.handle.resource_id
    assert "target-secret" not in target.handle.resource_id


@pytest.mark.parametrize("prefix", ["", "TARGET_"], ids=["unprefixed", "prefixed"])
def test_environment_provider_normalizes_endpoint_before_computing_identity(prefix: str) -> None:
    # GIVEN two configurations differing only in a runtime-normalized endpoint slash
    environ = {
        f"{prefix}S3_ENDPOINT": "https://s3.test",
        f"{prefix}S3_ACCESS_KEY": CREDS.access_key,
        f"{prefix}S3_SECRET_KEY": CREDS.secret_key,
    }
    expected = provider_from_environment(environ, prefix)

    # WHEN constructing both providers, THEN they share resource and ownership identities
    actual = provider_from_environment({**environ, f"{prefix}S3_ENDPOINT": "https://s3.test/"}, prefix)
    assert isinstance(expected, StaticS3Provider)
    assert isinstance(actual, StaticS3Provider)
    assert actual.endpoint == expected.endpoint
    assert actual.handle == expected.handle
    assert actual._bucket_owner == expected._bucket_owner


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

    target = provider_from_environment(target_environment)
    neighbor = provider_from_environment(neighbor_environment)

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


@pytest.mark.parametrize("credential_kind", ["application", "cleanup"], ids=["application-secret", "cleanup-secret"])
def test_direct_static_handles_include_secrets_without_changing_ownership(credential_kind: str) -> None:
    # GIVEN matching non-secret identities but different application or cleanup secrets
    original = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, cleanup_credentials=CLEANUP_CREDS)
    credentials = (
        S3Credentials(CREDS.access_key, "different-application-secret") if credential_kind == "application" else CREDS
    )
    cleanup_credentials = (
        S3Credentials(CLEANUP_CREDS.access_key, "different-cleanup-secret")
        if credential_kind == "cleanup"
        else CLEANUP_CREDS
    )

    # WHEN constructed directly, THEN credential configurations differ but persistent ownership is unchanged
    other = StaticS3Provider("https://s3.test", NAMESPACE, credentials, cleanup_credentials=cleanup_credentials)
    assert original.handle.resource_id != other.handle.resource_id
    assert original._bucket_owner == other._bucket_owner
    assert credentials.secret_key not in other.handle.resource_id
    assert cleanup_credentials.secret_key not in other.handle.resource_id


@pytest.mark.parametrize("cleanup_credentials", [None, CREDS, CLEANUP_CREDS], ids=["implicit", "same", "separate"])
def test_direct_static_handle_matches_environment_configuration(cleanup_credentials: S3Credentials | None) -> None:
    # GIVEN the same effective configuration constructed directly and through the environment boundary
    environ = {
        "S3_ENDPOINT": "https://s3.test/",
        "S3_BUCKET_NAMESPACE": NAMESPACE,
        "S3_ACCESS_KEY": CREDS.access_key,
        "S3_SECRET_KEY": CREDS.secret_key,
    }
    if cleanup_credentials is not None:
        environ.update(
            S3_CLEANUP_ACCESS_KEY=cleanup_credentials.access_key, S3_CLEANUP_SECRET_KEY=cleanup_credentials.secret_key
        )
    direct = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, cleanup_credentials=cleanup_credentials)

    # WHEN loading it, THEN both entrypoints use one canonical handle and ownership identity
    loaded = provider_from_environment(environ)
    assert isinstance(loaded, StaticS3Provider)
    assert loaded.handle == direct.handle
    assert loaded._bucket_owner == direct._bucket_owner


def test_direct_static_handle_preserves_explicit_identity() -> None:
    provider = StaticS3Provider("https://s3.test", NAMESPACE, CREDS, identity="explicit-provider")
    assert provider.handle.identity == "explicit-provider"
