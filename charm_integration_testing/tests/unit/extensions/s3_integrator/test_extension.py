# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from extensions import S3IntegratorExtension
from juju import JujuClient, JujuModelHandle
from juju.backend import JujuTask
from resource_registry import ResourceRegistry
from s3_storage import (
    S3Bucket,
    S3BucketHandle,
    S3Credentials,
    S3Provider,
    S3ProviderRegistry,
    S3ResourceRegistry,
    S3StorageHandle,
)

from ..shared import JujuStub as JujuStubBase

MODEL = JujuModelHandle(controller="controller", model="model")
TARGET_CLOUD = "target-cloud"


@dataclass
class JujuStub(JujuStubBase):
    applications: dict[str, str] = field(default_factory=lambda: {"storage": "s3-integrator"})
    application_charm_calls: list[str] = field(default_factory=list)
    action_status: str = "completed"
    action_return_code: int = 0
    action_message: str = ""
    action_output: str = ""
    unit_count: int = 1
    scale_target: int | None = None
    num_units_calls: int = 0
    clouds: dict[JujuModelHandle, str] = field(default_factory=lambda: {MODEL: TARGET_CLOUD})
    cloud_lookups: list[JujuModelHandle] = field(default_factory=list)

    def cloud_for_model(self, model: JujuModelHandle) -> str:
        self.cloud_lookups.append(model)
        return self.clouds[model]

    def scale_application(self, model: JujuModelHandle, application: str, num: int) -> None:
        self.scale_target = num

    def num_units(self, model: JujuModelHandle, application: str) -> int:
        if self.scale_target is not None:
            self.num_units_calls += 1
            if self.num_units_calls >= 2:
                self.unit_count = self.scale_target
                self.scale_target = None
        return self.unit_count

    def application_charm(self, model: JujuModelHandle, application: str) -> str:
        self.application_charm_calls.append(application)
        return self.applications[application]

    def wait_application_settled(self, model: JujuModelHandle, application: str, timeout: timedelta | None) -> None:
        assert self.unit_count > 0, "A zero-unit application remains unknown, not settled"
        super().wait_application_settled(model, application, timeout)

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
        return JujuTask("id", self.action_return_code, self.action_status, self.action_message, self.action_output)


@dataclass
class StorageStub(S3Provider):
    def __init__(
        self,
        endpoint: str = "https://s3.test",
        namespace: str = "test",
        teardown_events: list[str] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self.namespace = namespace
        self.buckets: list[BucketStub] = []
        self.fail_next_provision = False
        self.teardown_events = teardown_events if teardown_events is not None else []

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
        bucket = BucketStub(
            S3BucketHandle(self.namespace, f"isolated-bucket-{self.namespace}-{len(self.buckets)}"),
            self.teardown_events,
        )
        bucket.fail_provision = self.fail_next_provision
        self.fail_next_provision = False
        self.buckets.append(bucket)
        return bucket

    def cleanup_stale(self, age: timedelta, now: datetime | None = None) -> list[str]:
        return []

    def cleanup_pending_resources(self) -> None:
        pass


class BucketStub(S3Bucket):
    def __init__(self, handle: S3BucketHandle, teardown_events: list[str]) -> None:
        self._handle = handle
        self._teardown_events = teardown_events
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
        self._teardown_events.append("bucket")


def make_registry(models: tuple[JujuModelHandle, ...] = (MODEL,)) -> ResourceRegistry:
    registry = ResourceRegistry([], logging.getLogger(__name__))
    for model in models:
        registry.register(handle=model)
    return registry


def make_integrator_client(
    backend: JujuStub,
    providers: Mapping[str, S3Provider],
    registry: ResourceRegistry,
    cleanup_buckets: bool = True,
) -> JujuClient:
    scopes: dict[S3StorageHandle, S3ResourceRegistry] = {}

    def resources_for_provider(provider: S3Provider) -> S3ResourceRegistry:
        if provider.handle not in scopes:
            scopes[provider.handle] = S3ResourceRegistry(provider, registry, cleanup_buckets)
        return scopes[provider.handle]

    def provider_factory(path: Path) -> S3Provider:
        return providers[str(path)]

    provider_registry = S3ProviderRegistry({cloud: Path(cloud) for cloud in providers}, provider_factory)
    return JujuClient(
        backend,
        logging.getLogger(__name__),
        extensions=[S3IntegratorExtension(backend, provider_registry, resources_for_provider)],
    )


def test_resource_registry_registers_any_provider_implementation() -> None:
    registry = ResourceRegistry([], logging.getLogger(__name__))
    provider = StorageStub()
    s3_registry = S3ResourceRegistry(provider, registry, cleanup_buckets=True)

    s3_registry.new_bucket()
    bucket = provider.buckets[0]

    assert registry.is_registered(bucket.handle)
    assert registry.children_of(provider.handle) == [bucket.handle]
    registry.teardown_all()
    assert bucket.cleanup_calls == 1


@pytest.mark.parametrize("is_k8s", [True, False], ids=["kubernetes", "machine"])
def test_deploy_configures_integrator_on_either_substrate(tmp_path: Path, is_k8s: bool) -> None:
    # GIVEN an integrator and storage injected through the Juju client
    backend = JujuStub(is_k8s=is_k8s)
    storage = StorageStub()
    registry = make_registry()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)
    # WHEN the deployment lifecycle runs twice
    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)
    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)
    # THEN credentials and configuration are applied only once without deploying another charm
    assert len(storage.buckets) == 1
    assert storage.buckets[0].cleanup_calls == 0
    assert registry.is_registered(storage.buckets[0].handle)
    assert registry.children_of(storage.handle) == [storage.buckets[0].handle]
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
    assert backend.application_charm_calls == []
    assert len(backend.waited_scaled) == len(backend.waited_settled) == 1
    registry.teardown_all()
    assert storage.buckets[0].cleanup_calls == 1


def test_provider_resources_are_independent_of_juju_models() -> None:
    backend = JujuStub()
    storage = StorageStub()
    registry = make_registry()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)

    client.deploy_bundle_file("bundle.yaml", MODEL)

    bucket = storage.buckets[0]
    assert registry.is_registered(bucket.handle)
    assert registry.children_of(MODEL) == []
    registry.teardown_all()
    assert bucket.cleanup_calls == 1


def test_zero_unit_machine_integrator_is_not_provisioned(tmp_path: Path) -> None:
    backend = JujuStub(is_k8s=False, unit_count=0)
    storage = StorageStub()
    registry = ResourceRegistry([], logging.getLogger(__name__))
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)

    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)

    assert storage.buckets == []
    assert backend.actions == []
    assert backend.configured_applications == []
    assert backend.waited_scaled
    assert backend.waited_settled == []


def test_zero_unit_integrator_is_configured_after_scaling_up(tmp_path: Path) -> None:
    backend = JujuStub(is_k8s=False, unit_count=0)
    storage = StorageStub()
    registry = make_registry()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)

    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)

    assert storage.buckets == []
    client.scale_application("storage", 1, MODEL)

    assert len(storage.buckets) == 1
    assert backend.configured_applications[0][2]["bucket"] == storage.buckets[0].handle.name


def test_zero_unit_integrator_needs_no_provider_until_scale_up(tmp_path: Path) -> None:
    # GIVEN an unconfigured zero-unit integrator without S3 configuration
    backend = JujuStub(is_k8s=False, unit_count=0)
    registry = make_registry()
    client = make_integrator_client(backend, {}, registry)

    # WHEN deploying it, THEN cloud/provider resolution and bucket creation are deferred
    client.deploy_bundle_file(str(tmp_path / "bundle.yaml"), MODEL)
    assert backend.cloud_lookups == []
    assert backend.actions == []
    assert registry.registered_handles() == [MODEL]

    # WHEN it is scaled up, THEN missing storage is reported at the point of use
    with pytest.raises(ValueError, match="No S3 configuration for Juju cloud 'target-cloud'"):
        client.scale_application("storage", 1, MODEL)
    assert backend.cloud_lookups == [MODEL]
    assert backend.actions == []
    assert registry.registered_handles() == [MODEL]


def test_scaling_integrator_only_reconciles_that_application() -> None:
    backend = JujuStub(applications={"storage": "s3-integrator", "other": "s3-integrator"})
    storage = StorageStub()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, make_registry())

    client.scale_application("storage", 1, MODEL)

    assert len(storage.buckets) == 1
    assert [application for _, application, _ in backend.configured_applications] == ["storage"]
    assert backend.waited_scaled == []
    assert len(backend.waited_settled) == 1


def test_scaling_integrator_to_zero_does_not_reconcile() -> None:
    backend = JujuStub()
    storage = StorageStub()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, make_registry())

    client.scale_application("storage", 0, MODEL)

    assert storage.buckets == []
    assert backend.waited_scaled == []
    assert backend.waited_settled == []


def test_unrelated_or_externally_configured_apps_need_no_credentials() -> None:
    backend = JujuStub(applications={"other": "ubuntu", "storage": "s3-integrator"})
    backend.configured_applications.append((MODEL.uri, "storage", {"endpoint": "https://external.test"}))
    client = make_integrator_client(backend, {}, ResourceRegistry([], logging.getLogger(__name__)))
    client.deploy_bundle_file("bundle.yaml", MODEL)
    assert backend.actions == []
    assert backend.cloud_lookups == []


def test_missing_credentials_fail_explicitly() -> None:
    client = make_integrator_client(JujuStub(), {}, ResourceRegistry([], logging.getLogger(__name__)))
    with pytest.raises(ValueError, match="No S3 configuration for Juju cloud 'target-cloud'.*S3_CONFIG_"):
        client.deploy_bundle_file("bundle.yaml", MODEL)


@pytest.mark.parametrize(("status", "code"), [("failed", 0), ("completed", 1)])
def test_failed_action_does_not_mark_integrator_configured(status: str, code: int) -> None:
    # GIVEN a backend that returns a failed task rather than raising
    backend = JujuStub(
        action_status=status,
        action_return_code=code,
        action_message="potential-secret-message",
        action_output="potential-secret-output",
    )
    storage = StorageStub()
    registry = make_registry()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)
    # WHEN credential synchronization fails
    with pytest.raises(RuntimeError, match="synchronization failed") as error:
        client.deploy_bundle_file("bundle.yaml", MODEL)
    assert f"model {MODEL.uri}" in str(error.value)
    assert "action id" in str(error.value)
    assert f"status {status}" in str(error.value)
    assert f"return code {code}" in str(error.value)
    assert "potential-secret" not in str(error.value)
    # THEN a later deploy can retry instead of incorrectly skipping configuration
    assert backend.configured_applications == []
    backend.action_status = "completed"
    backend.action_return_code = 0
    client.deploy_bundle_file("bundle.yaml", MODEL)
    assert len(backend.configured_applications) == 1
    assert len(storage.buckets) == 2
    assert registry.is_registered(storage.buckets[0].handle)
    assert registry.is_registered(storage.buckets[1].handle)


def test_bucket_is_registered_before_external_provisioning() -> None:
    backend = JujuStub()
    storage = StorageStub()
    registry = make_registry()
    storage.fail_next_provision = True
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry)

    with pytest.raises(RuntimeError, match="Bucket provisioning failed"):
        client.deploy_bundle_file("bundle.yaml", MODEL)

    bucket = storage.buckets[0]
    assert registry.is_registered(bucket.handle)
    registry.teardown_all()
    assert bucket.cleanup_calls == 1


def test_resumed_runs_register_bucket_without_destroyer() -> None:
    backend = JujuStub()
    storage = StorageStub()
    registry = make_registry()
    client = make_integrator_client(backend, {TARGET_CLOUD: storage}, registry, cleanup_buckets=False)

    client.deploy_bundle_file("bundle.yaml", MODEL)
    registry.teardown_all()

    assert storage.buckets[0].cleanup_calls == 0


def test_same_model_name_on_different_controllers_uses_its_current_cloud() -> None:
    neighbor = JujuModelHandle(controller="neighbor-controller", model=MODEL.model)
    backend = JujuStub(clouds={MODEL: TARGET_CLOUD, neighbor: "neighbor-cloud"})
    target_storage = StorageStub("https://target-s3.test", "target")
    neighbor_storage = StorageStub("https://neighbor-s3.test", "neighbor")
    registry = make_registry(models=(MODEL, neighbor))
    client = make_integrator_client(
        backend, {TARGET_CLOUD: target_storage, "neighbor-cloud": neighbor_storage}, registry
    )

    client.deploy_bundle_file("bundle.yaml", MODEL)
    client.deploy_bundle_file("bundle.yaml", neighbor)

    configured_endpoints = {model_uri: config["endpoint"] for model_uri, _, config in backend.configured_applications}
    assert configured_endpoints == {
        MODEL.uri: target_storage.endpoint,
        neighbor.uri: neighbor_storage.endpoint,
    }
    assert backend.cloud_lookups == [MODEL, neighbor]


def test_model_cloud_is_resolved_again_after_topology_changes() -> None:
    # GIVEN a configured integrator on the target cloud
    backend = JujuStub()
    target = StorageStub(namespace="target")
    neighbor = StorageStub(namespace="neighbor")
    client = make_integrator_client(backend, {TARGET_CLOUD: target, "neighbor-cloud": neighbor}, make_registry())
    client.deploy_bundle_file("bundle.yaml", MODEL)

    # WHEN another integrator appears after the model's cloud changes
    backend.clouds[MODEL] = "neighbor-cloud"
    backend.applications["new-storage"] = "s3-integrator"
    client.deploy_bundle_file("bundle.yaml", MODEL)

    # THEN the new integrator uses the current cloud, without a model-name cache
    assert len(target.buckets) == len(neighbor.buckets) == 1
    assert backend.configured_applications[-1][1] == "new-storage"
    assert backend.configured_applications[-1][2]["bucket"] == neighbor.buckets[0].handle.name
    assert backend.cloud_lookups == [MODEL, MODEL]
