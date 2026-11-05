# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, cast
from urllib.parse import urlsplit
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from .identity import bucket_owner_identity, provider_identity
from .provider import S3Bucket, S3Credentials, S3Provider

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef, TagTypeDef

S3_PATH = "charmqa"
S3_OWNER_TAG = "charmqa-storage-owner"


@dataclass(frozen=True)
class S3StorageHandle:
    namespace: str
    identity: str | None = None

    def __post_init__(self) -> None:
        if self.identity == "":
            raise ValueError("S3 storage identity must not be empty")

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


class StaticS3Provider(S3Provider):
    """Provide isolated buckets using one pre-provisioned S3 identity."""

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        credentials: S3Credentials,
        region: str = "us-east-1",
        client_factory: Callable[[S3Credentials], S3Client] | None = None,
        identity: str | None = None,
    ) -> None:
        url = urlsplit(endpoint)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("S3_ENDPOINT must be an HTTP(S) URL without embedded credentials")
        if not namespace:
            raise ValueError("S3_BUCKET_NAMESPACE must not be empty")
        self._endpoint = endpoint.rstrip("/")
        self._region = region
        self._credentials = credentials
        attributes = ("static", self._endpoint, namespace, region, credentials.access_key)
        # Preserve tags already written when the shared key filled both identity slots.
        self._bucket_owner = bucket_owner_identity(*attributes, credentials.access_key)
        self._handle = S3StorageHandle(
            namespace, identity if identity is not None else provider_identity(*attributes, credentials.secret_key)
        )
        self._prefix = f"charmqa-s3-{hashlib.sha256(namespace.encode()).hexdigest()[:8]}-"
        self._client_factory = client_factory or self._make_client

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def region(self) -> str:
        return self._region

    @property
    def handle(self) -> S3StorageHandle:
        return self._handle

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
            self._credentials,
            self._client_factory,
            self._region,
            self._bucket_owner,
        )

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


class _S3Bucket(S3Bucket):
    def __init__(
        self,
        handle: S3BucketHandle,
        credentials: S3Credentials,
        client_factory: Callable[[S3Credentials], S3Client],
        region: str,
        bucket_owner: str,
    ) -> None:
        self._handle = handle
        self._credentials = credentials
        self._client_factory = client_factory
        self._region = region
        self._bucket_owner = bucket_owner
        self._create_attempted = False

    @property
    def handle(self) -> S3BucketHandle:
        return self._handle

    def provision(self) -> tuple[str, S3Credentials]:
        with closing(self._client_factory(self._credentials)) as client:
            if self._create_attempted:
                try:
                    client.head_bucket(Bucket=self._handle.name)
                except ClientError as exc:
                    error = exc.response.get("Error", {})
                    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if error.get("Code") not in {"404", "NoSuchBucket", "NotFound"} and status_code != 404:
                        raise
                    self._create_bucket(client)
            else:
                self._create_attempted = True
                self._create_bucket(client)

            tags: list[TagTypeDef] = [{"Key": S3_OWNER_TAG, "Value": self._bucket_owner}]
            client.put_bucket_tagging(Bucket=self._handle.name, Tagging={"TagSet": tags})
            client.put_object(Bucket=self._handle.name, Key=f"{S3_PATH}/", Body=b"")
            client.head_bucket(Bucket=self._handle.name)
        return self._handle.name, self._credentials

    def _create_bucket(self, client: S3Client) -> None:
        if self._region == "us-east-1":
            client.create_bucket(Bucket=self._handle.name)
        else:
            # Compatible S3 endpoints may use region names outside boto's AWS literals.
            client.create_bucket(
                Bucket=self._handle.name,
                CreateBucketConfiguration={"LocationConstraint": cast(Any, self._region)},
            )

    def cleanup(self) -> None:
        if self._create_attempted:
            with closing(self._client_factory(self._credentials)) as client:
                StaticS3Provider._delete_bucket(client, self._handle.name)
