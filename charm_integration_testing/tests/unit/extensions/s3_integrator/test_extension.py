# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from extensions import S3IntegratorExtension
from juju import JujuClient, JujuModelHandle
from juju.backend import JujuTask
from resource_registry import ResourceRegistry
from s3_storage import S3Bucket, S3BucketHandle, S3Credentials, S3Provider, S3StorageHandle

from ..shared import JujuStub as JujuStubBase

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class JujuStub(JujuStubBase):
    applications: dict[str, str] = field(default_factory=lambda: {"storage": "s3-integrator"})
    action_status: str = "completed"
    action_return_code: int = 0

    def deploy_bundle_file(
        self,
        model: JujuModelHandle,
        bundle: str,
        timeout: timedelta | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        pass

    def run_action(self, model: JujuModelHandle, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        super().run_action(model, unit, action, params)
        return JujuTask("id", self.action_return_code, self.action_status, "", "")


class StorageStub(S3Provider):
    def __init__(self, endpoint: str = "https://s3.test", namespace: str = "test") -> None:
        self._endpoint = endpoint
        self.namespace = namespace
        self.buckets: list[BucketStub] = []
        self.fail_next_provision = False

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def region(self) -> str:
        return "us-east-1"

    @property
    def handle(self) -> S3StorageHandle:
        return S3StorageHandle(self.namespace)

    def new_bucket(self) -> S3Bucket:
        bucket = BucketStub(S3BucketHandle(self.namespace, f"isolated-bucket-{self.namespace}-{len(self.buckets)}"))
        bucket.fail_provision = self.fail_next_provision
        self.fail_next_provision = False
        self.buckets.append(bucket)
        return bucket

    def cleanup_stale(self, age: timedelta, now: datetime | None = None) -> list[str]:
        return []


class BucketStub(S3Bucket):
    def __init__(self, handle: S3BucketHandle) -> None:
        self._handle = handle
        self.cleanup_calls = 0
        self.fail_provision = False

    @property
    def handle(self) -> S3BucketHandle:
        return self._handle

    def provision(self) -> tuple[str, S3Credentials]:
        if self.fail_provision:
            raise RuntimeError("Bucket provisioning failed")
        return self._handle.name, S3Credentials("access", "secret")

    def cleanup(self) -> None:
        self.cleanup_calls += 1


@pytest.mark.parametrize("is_k8s", [True, False], ids=["kubernetes", "machine"])
def test_deploy_configures_integrator_on_either_substrate(tmp_path: Path, is_k8s: bool) -> None:
    # GIVEN an integrator and storage injected through the Juju client
    backend = JujuStub(is_k8s=is_k8s)
    storage = StorageStub()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(handle=storage.handle)
    extension = S3IntegratorExtension(backend, {MODEL.resource_id: storage}, registry, cleanup_buckets=True)
    client = JujuClient(backend, logging.getLogger(__name__), extensions=[extension])
    # WHEN the deployment lifecycle runs twice
    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)
    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)
    # THEN credentials and configuration are applied only once without deploying another charm
    assert len(storage.buckets) == 1
    assert storage.buckets[0].cleanup_calls == 0
    assert registry.is_registered(storage.buckets[0].handle)
    assert backend.actions == [
        (MODEL.uri, "storage/leader", "sync-s3-credentials", {"access-key": "access", "secret-key": "secret"})
    ]
    assert backend.configured_applications == [
        (
            MODEL.uri,
            "storage",
            {
                "endpoint": "https://s3.test",
                "bucket": storage.buckets[0].handle.name,
                "path": "charmqa",
                "region": "us-east-1",
                "s3-uri-style": "path",
                "s3-api-version": "s3v4",
            },
        )
    ]
    assert backend.deployed == []
    assert len(backend.waited_scaled) == len(backend.waited_settled) == 1
    registry.teardown_all()
    assert storage.buckets[0].cleanup_calls == 1


def test_unrelated_or_externally_configured_apps_need_no_credentials() -> None:
    backend = JujuStub(applications={"other": "ubuntu", "storage": "s3-integrator"})
    backend.configured_applications.append((MODEL.uri, "storage", {"endpoint": "https://external.test"}))
    S3IntegratorExtension(
        backend, {}, ResourceRegistry([], logging.getLogger(__name__)), cleanup_buckets=True
    ).post_deploy(MODEL)
    assert backend.actions == []


def test_missing_credentials_fail_explicitly() -> None:
    with pytest.raises(RuntimeError, match="has no S3 provider"):
        S3IntegratorExtension(
            JujuStub(), {}, ResourceRegistry([], logging.getLogger(__name__)), cleanup_buckets=True
        ).post_deploy(MODEL)


@pytest.mark.parametrize(("status", "code"), [("failed", 0), ("completed", 1)])
def test_failed_action_does_not_mark_integrator_configured(status: str, code: int) -> None:
    # GIVEN a backend that returns a failed task rather than raising
    backend = JujuStub(action_status=status, action_return_code=code)
    storage = StorageStub()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(handle=storage.handle)
    extension = S3IntegratorExtension(backend, {MODEL.resource_id: storage}, registry, cleanup_buckets=True)
    # WHEN credential synchronization fails
    with pytest.raises(RuntimeError, match="synchronization failed"):
        extension.post_deploy(MODEL)
    # THEN a later deploy can retry instead of incorrectly skipping configuration
    assert backend.configured_applications == []
    backend.action_status = "completed"
    backend.action_return_code = 0
    extension.post_deploy(MODEL)
    assert len(backend.configured_applications) == 1
    assert len(storage.buckets) == 2
    assert registry.is_registered(storage.buckets[0].handle)
    assert registry.is_registered(storage.buckets[1].handle)


def test_bucket_is_registered_before_external_provisioning() -> None:
    backend = JujuStub()
    storage = StorageStub()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(handle=storage.handle)
    storage.fail_next_provision = True
    extension = S3IntegratorExtension(backend, {MODEL.resource_id: storage}, registry, cleanup_buckets=True)

    with pytest.raises(RuntimeError, match="Bucket provisioning failed"):
        extension.post_deploy(MODEL)

    bucket = storage.buckets[0]
    assert registry.is_registered(bucket.handle)
    registry.teardown_all()
    assert bucket.cleanup_calls == 1


def test_resumed_runs_register_bucket_without_destroyer() -> None:
    backend = JujuStub()
    storage = StorageStub()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(handle=storage.handle)
    extension = S3IntegratorExtension(backend, {MODEL.resource_id: storage}, registry, cleanup_buckets=False)

    extension.post_deploy(MODEL)
    registry.teardown_all()

    assert storage.buckets[0].cleanup_calls == 0


def test_each_model_uses_its_environment_provider() -> None:
    backend = JujuStub()
    neighbor = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")
    target_storage = StorageStub("https://target-s3.test", "target")
    neighbor_storage = StorageStub("https://neighbor-s3.test", "neighbor")
    registry = ResourceRegistry([], logging.getLogger(__name__))
    registry.register(handle=target_storage.handle)
    registry.register(handle=neighbor_storage.handle)
    extension = S3IntegratorExtension(
        backend,
        {MODEL.resource_id: target_storage, neighbor.resource_id: neighbor_storage},
        registry,
        cleanup_buckets=True,
    )

    extension.post_deploy(MODEL)
    extension.post_deploy(neighbor)

    configured_endpoints = {model_uri: config["endpoint"] for model_uri, _, config in backend.configured_applications}
    assert configured_endpoints == {
        MODEL.uri: target_storage.endpoint,
        neighbor.uri: neighbor_storage.endpoint,
    }


def test_migrated_model_provider_is_shared_across_extensions() -> None:
    backend = JujuStub()
    migrated_model = JujuModelHandle(controller="neighbor-controller", model=MODEL.model)
    storage = StorageStub("https://source-s3.test", "source")
    providers: dict[str, S3Provider] = {MODEL.resource_id: storage}
    extension = S3IntegratorExtension(
        backend,
        providers,
        ResourceRegistry([], logging.getLogger(__name__)),
        cleanup_buckets=True,
    )

    extension.post_migrate_model(MODEL.model, MODEL.controller, migrated_model.controller)
    # JujuClient creates a fresh extension for each test, but the session provider map is shared.
    new_extension = S3IntegratorExtension(
        backend,
        providers,
        ResourceRegistry([], logging.getLogger(__name__)),
        cleanup_buckets=True,
    )
    new_extension.post_deploy(migrated_model)

    assert backend.configured_applications[0][2]["endpoint"] == storage.endpoint


def test_migration_does_not_replace_target_provider() -> None:
    backend = JujuStub()
    migrated_model = JujuModelHandle(controller="neighbor-controller", model=MODEL.model)
    source_storage = StorageStub("https://source-s3.test", "source")
    target_storage = StorageStub("https://target-s3.test", "target")
    providers: dict[str, S3Provider] = {
        MODEL.resource_id: source_storage,
        migrated_model.resource_id: target_storage,
    }
    extension = S3IntegratorExtension(
        backend,
        providers,
        ResourceRegistry([], logging.getLogger(__name__)),
        cleanup_buckets=True,
    )

    extension.post_migrate_model(MODEL.model, MODEL.controller, migrated_model.controller)
    extension.post_deploy(migrated_model)

    assert backend.configured_applications[0][2]["endpoint"] == target_storage.endpoint
