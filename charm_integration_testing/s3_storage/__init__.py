# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from .provider import CredentialProvider, S3Bucket, S3Credentials, S3Provider, StaticCredentialProvider
from .registry import S3ProviderRegistry
from .storage import S3BucketHandle, S3Storage, S3StorageHandle, StaticS3Provider
from .swift import KeystoneClient, SwiftCredentialProvider, SwiftCredentials, SwiftS3Provider

__all__ = [
    "CredentialProvider",
    "KeystoneClient",
    "S3Bucket",
    "S3BucketHandle",
    "S3Credentials",
    "S3Provider",
    "S3ProviderRegistry",
    "S3Storage",
    "S3StorageHandle",
    "StaticCredentialProvider",
    "StaticS3Provider",
    "SwiftCredentialProvider",
    "SwiftCredentials",
    "SwiftS3Provider",
]
