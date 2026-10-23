# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
from dataclasses import dataclass
from functools import partial

from resource_registry import ResourceHandle, ResourceRegistry
from s3_storage import S3Credentials
from s3_storage.identity import bucket_owner_identity

from .credentials import KeystoneClient


@dataclass(frozen=True)
class KeystoneCredentialHandle:
    identity: str

    @property
    def resource_id(self) -> str:
        return f"keystone:credential:{self.identity}"

    @property
    def resource_type(self) -> str:
        return "keystone:credential"

    @property
    def path_segment(self) -> str:
        return f"keystone-credential-{hashlib.sha256(self.identity.encode()).hexdigest()[:12]}"


class KeystoneResourceRegistry:
    """Register issued EC2 credentials in the shared session resource registry."""

    def __init__(self, resource_registry: ResourceRegistry, cleanup_credentials: bool) -> None:
        self._resource_registry = resource_registry
        self._cleanup_credentials = cleanup_credentials

    def create_credentials(self, keystone: KeystoneClient, parent: ResourceHandle) -> S3Credentials:
        """Issue and register a credential before the caller can use it."""
        if not self._resource_registry.is_registered(parent):
            raise ValueError(f"Parent '{parent.resource_id}' is not registered")
        credentials = keystone.create_credentials()
        identity = keystone.credentials
        handle = KeystoneCredentialHandle(
            bucket_owner_identity(
                "keystone",
                identity.auth_endpoint(),
                identity.username,
                identity.user_domain_name,
                identity.project_name,
                identity.project_domain_name,
                credentials.access_key,
            )
        )
        self._resource_registry.register(
            handle=handle,
            destroyer=partial(keystone.delete_credentials, credentials.access_key)
            if self._cleanup_credentials
            else None,
            parent=parent,
        )
        return credentials
