# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, cast
from urllib.parse import urlsplit
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .provider import CredentialProvider, S3Bucket, S3Credentials, S3Provider, StaticCredentialProvider

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef, TagTypeDef

S3_PATH = "charmqa"
S3_OWNER_TAG = "charmqa-storage-owner"
S3_ACCESS_KEY_TAG = "charmqa-ec2-access"


def _stable_bucket_owner(*values: str) -> str:
    """Return a stable opaque ownership marker for bucket tags."""
    serialized = json.dumps(values, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class S3StorageHandle:
    namespace: str
    identity: str | None = None

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


class S3Storage(S3Provider):
    """Shared bucket lifecycle for concrete S3 providers."""

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        credential_provider: CredentialProvider,
        region: str = "us-east-1",
        client_factory: Callable[[S3Credentials], S3Client] | None = None,
        identity: str | None = None,
        bucket_owner_identity: str | None = None,
    ) -> None:
        url = urlsplit(endpoint)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("S3_ENDPOINT must be an HTTP(S) URL without embedded credentials")
        if not namespace:
            raise ValueError("S3_BUCKET_NAMESPACE must not be empty")
        self._endpoint = endpoint.rstrip("/")
        self._region = region
        self._credential_provider = credential_provider
        if bucket_owner_identity is not None:
            self._bucket_owner = bucket_owner_identity
        elif credential_provider.ownership_identity is not None:
            self._bucket_owner = _stable_bucket_owner(
                "s3-storage", self._endpoint, namespace, region, *credential_provider.ownership_identity
            )
        else:
            raise ValueError(
                "S3 credential providers used with S3Storage must define ownership_identity or provide "
                "bucket_owner_identity"
            )
        self._handle = S3StorageHandle(namespace, identity if identity is not None else self._bucket_owner)
        self._prefix = f"charmqa-s3-{hashlib.sha256(namespace.encode()).hexdigest()[:8]}-"
        self._client_factory = client_factory or self._make_client
        self._pending_revocations: set[str] = set()
        self._pending_bucket_deletions: set[_S3Bucket] = set()

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def region(self) -> str:
        return self._region

    @property
    def handle(self) -> S3StorageHandle:
        return self._handle

    def _bucket_destroyer(self, bucket: S3Bucket, destroy_on_teardown: bool) -> Callable[[], None] | None:
        if not destroy_on_teardown:
            return None

        def cleanup_bucket() -> None:
            try:
                bucket.cleanup()
            except Exception as cleanup_error:
                try:
                    self.cleanup_pending_resources()
                except Exception as retry_error:
                    raise retry_error from cleanup_error

        return cleanup_bucket

    def _make_client(self, credentials: S3Credentials) -> S3Client:
        client: S3Client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 1},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return client

    def new_bucket(self) -> S3Bucket:
        name = f"{self._prefix}{int(datetime.now(timezone.utc).timestamp())}-{uuid4().hex[:16]}"
        return _S3Bucket(
            S3BucketHandle(self._handle.namespace, name),
            self._credential_provider,
            self._client_factory,
            self._region,
            self._pending_revocations,
            self._pending_bucket_deletions,
            self._bucket_owner,
        )

    def cleanup_pending_resources(self) -> None:
        """Retry bucket deletion and credential revocation left pending by bucket cleanup."""
        failures: list[Exception] = []
        for bucket in list(self._pending_bucket_deletions):
            try:
                bucket.cleanup()
            except Exception as exc:
                failures.append(exc)

        pending_bucket_keys = {
            bucket._credentials.access_key
            for bucket in self._pending_bucket_deletions
            if bucket._credentials is not None and not bucket._credentials_revoked
        }
        for access_key in list(self._pending_revocations):
            if access_key in pending_bucket_keys:
                continue
            try:
                self._credential_provider.revoke(access_key)
            except Exception as exc:
                failures.append(exc)
            else:
                self._pending_revocations.discard(access_key)
        if failures:
            raise RuntimeError(f"Failed to clean {len(failures)} pending S3 resource(s)") from failures[0]

    @staticmethod
    def _delete_bucket(client: S3Client, name: str) -> None:
        """Remove multipart uploads, every version and delete marker, then the bucket."""
        try:
            for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=name):
                for upload in page.get("Uploads", []):
                    client.abort_multipart_upload(Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"])
            for versions_page in client.get_paginator("list_object_versions").paginate(Bucket=name):
                objects: list[ObjectIdentifierTypeDef] = [
                    {"Key": obj["Key"], "VersionId": obj["VersionId"]}
                    for obj in [*versions_page.get("Versions", []), *versions_page.get("DeleteMarkers", [])]
                ]
                for start in range(0, len(objects), 1000):
                    result = client.delete_objects(Bucket=name, Delete={"Objects": objects[start : start + 1000]})
                    if result.get("Errors"):
                        raise RuntimeError(f"S3 object deletion failed in bucket {name}")
            client.delete_bucket(Bucket=name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise

    def cleanup_stale(self, age: timedelta, now: datetime | None = None) -> list[str]:
        """Sweep only timestamped harness buckets belonging to this environment."""
        if age <= timedelta(0):
            raise ValueError("Cleanup age must be positive")
        now = now or datetime.now(timezone.utc)
        pattern = re.compile(rf"{re.escape(self._prefix)}(\d{{10}})-[0-9a-f]{{16}}")
        client: S3Client | None = None
        removed: list[str] = []
        failures: list[str] = []
        first_failure: Exception | None = None
        try:
            client = self._client_factory(self._credential_provider.cleanup_credentials)
            for page in client.get_paginator("list_buckets").paginate():
                for bucket in page.get("Buckets", []):
                    name = bucket["Name"]
                    match = pattern.fullmatch(name)
                    if match is None or now - datetime.fromtimestamp(int(match[1]), timezone.utc) <= age:
                        continue
                    try:
                        try:
                            tags = client.get_bucket_tagging(Bucket=name)["TagSet"]
                        except ClientError as exc:
                            code = exc.response.get("Error", {}).get("Code")
                            if code == "NoSuchBucket":
                                continue
                            if code != "NoSuchTagSet":
                                raise
                            raise RuntimeError("Missing ownership tags; retaining bucket") from exc
                        owner = next((tag["Value"] for tag in tags if tag["Key"] == S3_OWNER_TAG), None)
                        if not owner:
                            raise RuntimeError("Missing ownership tag; retaining bucket")
                        if owner != self._bucket_owner:
                            continue
                        if self._credential_provider.per_bucket_credentials:
                            access_key = next((tag["Value"] for tag in tags if tag["Key"] == S3_ACCESS_KEY_TAG), None)
                            if not access_key:
                                raise RuntimeError("Missing EC2 access-key tag; retaining bucket")
                            self._credential_provider.revoke(access_key)
                        self._delete_bucket(client, name)
                        removed.append(name)
                    except Exception as exc:
                        failures.append(f"{name}: {type(exc).__name__}")
                        if first_failure is None:
                            first_failure = exc
        finally:
            if client is not None:
                client.close()
        if failures:
            raise RuntimeError("Failed to clean stale S3 resources: " + "; ".join(failures)) from first_failure
        return removed


class _S3Bucket(S3Bucket):
    def __init__(
        self,
        handle: S3BucketHandle,
        credential_provider: CredentialProvider,
        client_factory: Callable[[S3Credentials], S3Client],
        region: str,
        pending_revocations: set[str],
        pending_bucket_deletions: set[_S3Bucket],
        bucket_owner: str,
    ) -> None:
        self._handle = handle
        self._credential_provider = credential_provider
        self._client_factory = client_factory
        self._region = region
        self._pending_revocations = pending_revocations
        self._pending_bucket_deletions = pending_bucket_deletions
        self._bucket_owner = bucket_owner
        self._credentials: S3Credentials | None = None
        self._credentials_revoked = False
        self._client: S3Client | None = None
        self._create_attempted = False

    @property
    def handle(self) -> S3BucketHandle:
        return self._handle

    def provision(self) -> tuple[str, S3Credentials]:
        if self._credentials is None:
            self._credentials = self._credential_provider.issue()
            if self._credential_provider.per_bucket_credentials:
                self._pending_revocations.add(self._credentials.access_key)
        if self._client is None:
            try:
                self._client = self._client_factory(
                    self._credential_provider.provisioning_credentials(self._credentials)
                )
            except Exception as client_error:
                try:
                    access_key = self._credentials.access_key
                    self._credential_provider.revoke(access_key)
                except Exception as revoke_error:
                    raise RuntimeError(
                        "S3 client initialization failed and the issued credentials could not be revoked "
                        f"({type(revoke_error).__name__}; client error: {type(client_error).__name__})"
                    ) from revoke_error
                self._pending_revocations.discard(access_key)
                self._credentials = None
                self._credentials_revoked = False
                raise

        if self._create_attempted:
            try:
                self._client.head_bucket(Bucket=self._handle.name)
            except ClientError as exc:
                error = exc.response.get("Error", {})
                status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if error.get("Code") not in {"404", "NoSuchBucket", "NotFound"} and status_code != 404:
                    raise
                self._create_bucket()
        else:
            self._create_attempted = True
            self._create_bucket()

        if self._credential_provider.per_bucket_credentials:
            tags: list[TagTypeDef] = [{"Key": S3_OWNER_TAG, "Value": self._bucket_owner}]
            tags.append({"Key": S3_ACCESS_KEY_TAG, "Value": self._credentials.access_key})
            self._client.put_bucket_tagging(Bucket=self._handle.name, Tagging={"TagSet": tags})
        else:
            self._client.put_bucket_tagging(
                Bucket=self._handle.name, Tagging={"TagSet": [{"Key": S3_OWNER_TAG, "Value": self._bucket_owner}]}
            )
        self._client.put_object(Bucket=self._handle.name, Key=f"{S3_PATH}/", Body=b"")
        self._client.head_bucket(Bucket=self._handle.name)
        if self._credential_provider.per_bucket_credentials:
            self._pending_revocations.discard(self._credentials.access_key)
        return self._handle.name, self._credentials

    def _create_bucket(self) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("Cannot create an S3 bucket before initializing its client")
        if self._region == "us-east-1":
            client.create_bucket(Bucket=self._handle.name)
        else:
            # Compatible S3 endpoints may use region names outside boto's AWS literals.
            client.create_bucket(
                Bucket=self._handle.name,
                CreateBucketConfiguration={"LocationConstraint": cast(Any, self._region)},
            )

    def cleanup(self) -> None:
        if self._credentials is None:
            if self._client is not None:
                self._client.close()
            return

        completed = False
        try:
            if self._credential_provider.per_bucket_credentials:
                if not self._credentials_revoked:
                    self._credential_provider.revoke(self._credentials.access_key)
                    self._pending_revocations.discard(self._credentials.access_key)
                    self._credentials_revoked = True
                if self._create_attempted:
                    cleanup_client = self._client_factory(self._credential_provider.cleanup_credentials)
                    try:
                        S3Storage._delete_bucket(cleanup_client, self._handle.name)
                    finally:
                        cleanup_client.close()
            elif self._client is not None and self._create_attempted:
                S3Storage._delete_bucket(self._client, self._handle.name)
            completed = True
        except Exception:
            if self._create_attempted:
                self._pending_bucket_deletions.add(self)
            if self._credential_provider.per_bucket_credentials and self._credentials is not None:
                if not self._credentials_revoked:
                    self._pending_revocations.add(self._credentials.access_key)
            raise
        finally:
            if self._client is not None and (
                self._credential_provider.per_bucket_credentials or completed or not self._create_attempted
            ):
                self._client.close()
                self._client = None
        if completed:
            self._pending_bucket_deletions.discard(self)


class StaticS3Provider(S3Storage):
    """Provide isolated buckets using managed application and optional cleanup credentials."""

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        credentials: S3Credentials,
        region: str = "us-east-1",
        client_factory: Callable[[S3Credentials], S3Client] | None = None,
        identity: str | None = None,
        cleanup_credentials: S3Credentials | None = None,
    ) -> None:
        credential_provider = StaticCredentialProvider(credentials, cleanup_credentials)
        super().__init__(
            endpoint,
            namespace,
            credential_provider,
            region=region,
            client_factory=client_factory,
            identity=identity,
            bucket_owner_identity=_stable_bucket_owner(
                "static",
                endpoint.rstrip("/"),
                namespace,
                region,
                credentials.access_key,
                credential_provider.cleanup_credentials.access_key,
            ),
        )
