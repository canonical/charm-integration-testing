# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from s3_storage import CredentialProvider, S3BucketHandle, S3Credentials, S3Storage
from s3_storage.identity import bucket_owner_identity, provider_identity

from .credentials import KeystoneClient
from .resource_registry import KeystoneResourceRegistry


class SwiftCredentialProvider(CredentialProvider):
    """Per-bucket Keystone EC2 keys, with a persistent identity reserved for cleanup."""

    per_bucket_credentials = True

    def __init__(
        self,
        keystone: KeystoneClient,
        cleanup_credentials: S3Credentials,
        resources: KeystoneResourceRegistry | None = None,
    ) -> None:
        self.keystone = keystone
        self._cleanup_credentials = cleanup_credentials
        self._resources = resources

    @property
    def cleanup_credentials(self) -> S3Credentials:
        return self._cleanup_credentials

    @property
    def ownership_identity(self) -> tuple[str, ...]:
        credentials = self.keystone.credentials
        return (
            "swift",
            credentials.auth_endpoint(),
            credentials.username,
            credentials.user_domain_name,
            credentials.project_name,
            credentials.project_domain_name,
            credentials.storage_endpoint(),
            self.cleanup_credentials.access_key,
        )

    def issue(self, bucket: S3BucketHandle) -> S3Credentials:
        if self._resources is None:
            raise RuntimeError("Swift bucket provisioning requires a KeystoneResourceRegistry")
        return self._resources.create_credentials(self.keystone, parent=bucket)

    def revoke(self, access_key: str) -> None:
        self.keystone.delete_credentials(access_key)


class SwiftS3Provider(S3Storage):
    """Provide isolated S3 buckets through Swift's Keystone EC2 credentials."""

    def __init__(
        self,
        endpoint: str,
        namespace: str,
        keystone: KeystoneClient,
        cleanup_credentials: S3Credentials,
        region: str = "us-east-1",
        identity: str | None = None,
        *,
        resources: KeystoneResourceRegistry | None = None,
    ) -> None:
        self.keystone = keystone
        if identity is None:
            credentials = keystone.credentials
            identity = provider_identity(
                "swift",
                endpoint.rstrip("/"),
                namespace,
                region,
                credentials.auth_endpoint(),
                credentials.username,
                credentials.password,
                credentials.user_domain_name,
                credentials.project_name,
                credentials.project_domain_name,
                credentials.storage_endpoint(),
                cleanup_credentials.access_key,
                cleanup_credentials.secret_key,
            )
        super().__init__(
            endpoint,
            namespace,
            SwiftCredentialProvider(keystone, cleanup_credentials, resources),
            region=region,
            identity=identity,
            bucket_owner_identity=bucket_owner_identity(
                "swift",
                endpoint.rstrip("/"),
                namespace,
                region,
                keystone.credentials.auth_endpoint(),
                keystone.credentials.username,
                keystone.credentials.user_domain_name,
                keystone.credentials.project_name,
                keystone.credentials.project_domain_name,
                keystone.credentials.storage_endpoint(),
                cleanup_credentials.access_key,
            ),
        )
