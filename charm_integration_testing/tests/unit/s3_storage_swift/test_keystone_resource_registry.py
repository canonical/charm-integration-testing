# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import replace

import pytest
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import S3BucketHandle, S3Credentials
from s3_storage_swift import KeystoneClient, KeystoneCredentialHandle, KeystoneResourceRegistry, SwiftCredentials


class KeystoneStub(KeystoneClient):
    def __init__(self) -> None:
        self.credentials = SwiftCredentials(
            "https://identity.test/v3", "user", "password", "users", "project", "projects", "https://storage.test"
        )
        self.created = 0
        self.deleted: list[str] = []
        self.fail_delete = False

    def create_credentials(self) -> S3Credentials:
        self.created += 1
        return S3Credentials(f"access-{self.created}", f"secret-{self.created}")

    def delete_credentials(self, access_key: str) -> None:
        self.deleted.append(access_key)
        if self.fail_delete:
            raise RuntimeError("Credential revocation failed")


@pytest.mark.parametrize("cleanup", [True, False], ids=["new-controller", "existing-controller"])
def test_credentials_are_registered_as_bucket_children(cleanup: bool) -> None:
    # GIVEN a registered bucket and a Keystone resource adapter
    registry = ResourceRegistry([], logging.getLogger(__name__))
    parent = S3BucketHandle("test", "bucket")
    registry.register(parent)
    keystone = KeystoneStub()
    resources = KeystoneResourceRegistry(registry, cleanup)

    # WHEN credentials are issued, THEN they are tracked before returning to the caller
    credentials = resources.create_credentials(keystone, parent)
    handles = registry.children_of(parent)
    assert len(handles) == 1
    assert isinstance(handles[0], KeystoneCredentialHandle)
    assert credentials.access_key not in handles[0].resource_id
    assert credentials.secret_key not in repr(handles[0])

    # WHEN teardown runs, THEN only owned credentials are revoked
    registry.teardown_all()
    assert keystone.deleted == ([credentials.access_key] if cleanup else [])
    assert registry.registered_handles() == []


def test_unregistered_parent_is_rejected_before_issuing_credentials() -> None:
    # GIVEN no registered bucket
    registry = ResourceRegistry([], logging.getLogger(__name__))
    keystone = KeystoneStub()
    resources = KeystoneResourceRegistry(registry, True)

    # WHEN credential creation is requested, THEN no external state is created
    with pytest.raises(ValueError, match="Parent .* is not registered"):
        resources.create_credentials(keystone, S3BucketHandle("test", "bucket"))
    assert keystone.created == 0


def test_failed_revocation_does_not_skip_bucket_destruction() -> None:
    # GIVEN a credential whose revocation fails and its independently deletable bucket
    registry = ResourceRegistry([], logging.getLogger(__name__))
    events: list[str] = []
    parent = S3BucketHandle("test", "bucket")
    registry.register(parent, destroyer=lambda: events.append("bucket-deleted"))
    keystone = KeystoneStub()
    keystone.fail_delete = True
    credentials = KeystoneResourceRegistry(registry, True).create_credentials(keystone, parent)

    # WHEN teardown runs, THEN revocation is attempted once and bucket destruction still runs
    with pytest.warns(ResourceTeardownWarning, match="Credential revocation failed"):
        registry.teardown_all()
    assert keystone.deleted == [credentials.access_key]
    assert events == ["bucket-deleted"]
    assert registry.registered_handles() == []


def test_credential_handles_distinguish_keystone_accounts() -> None:
    # GIVEN different Keystone projects that return the same access-key string
    registry = ResourceRegistry([], logging.getLogger(__name__))
    parent = S3BucketHandle("test", "bucket")
    registry.register(parent)
    first, second = KeystoneStub(), KeystoneStub()
    second.credentials = replace(second.credentials, project_name="other-project")
    resources = KeystoneResourceRegistry(registry, True)

    # WHEN both credentials are registered, THEN their handles are independent
    resources.create_credentials(first, parent)
    resources.create_credentials(second, parent)
    assert len({handle.resource_id for handle in registry.children_of(parent)}) == 2
    registry.teardown_all()
    assert first.deleted == ["access-1"]
    assert second.deleted == ["access-1"]


def test_credential_handle_has_safe_path_segment() -> None:
    assert KeystoneCredentialHandle("identity/with:unsafe characters").path_segment.replace("-", "").isalnum()
