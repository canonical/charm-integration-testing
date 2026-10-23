# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
from dataclasses import dataclass
from uuid import uuid4

from resource_registry import ResourceHandle, ResourceRegistry
from s3_storage import S3Credentials

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
        """Register credential intent before issuance can create external state."""
        resource = _KeystoneCredential(keystone)
        self._resource_registry.register(
            handle=resource.handle,
            destroyer=resource.cleanup if self._cleanup_credentials else None,
            parent=parent,
        )
        return resource.provision()


class _KeystoneCredential:
    def __init__(self, keystone: KeystoneClient) -> None:
        self.handle = KeystoneCredentialHandle(uuid4().hex)
        self._keystone = keystone
        self._access_key: str | None = None

    def provision(self) -> S3Credentials:
        return self._keystone.create_credentials(on_access_key=self._record_access_key)

    def _record_access_key(self, access_key: str) -> None:
        self._access_key = access_key

    def cleanup(self) -> None:
        if self._access_key is not None:
            self._keystone.delete_credentials(self._access_key)
