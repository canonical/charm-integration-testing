# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from datetime import timedelta
from inspect import unwrap
from pathlib import Path
from typing import cast

import pytest
from juju import JujuClient, JujuModelHandle, JujuVersion
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes.config import ConfigException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClientRegistry
from test_suite import conftest
from test_suite import test_controller_restart as restart
from test_suite import test_model_controller_migration as migration

from ..extensions.shared import NullJujuBackend

TARGET = JujuModelHandle(controller="target", model="workload")
NEIGHBOR = JujuModelHandle(controller="neighbor", model="neighbor")


class CloudBackend(NullJujuBackend):
    def __init__(self, clouds: dict[str, str | None]) -> None:
        self.clouds = clouds
        self.resolved: list[JujuModelHandle] = []

    def kubernetes_cloud_for_model(self, model: JujuModelHandle) -> str | None:
        self.resolved.append(model)
        return self.clouds[model.uri]


class RecordingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error = error

    def version(self, model: JujuModelHandle) -> JujuVersion:
        return JujuVersion.parse("3.6.27")

    def reboot_model_controller(self, model: JujuModelHandle) -> None:
        self.calls.append(("restart", model))
        if self.error is not None:
            raise self.error

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta) -> None:
        self.calls.append(("idle", models, timeout))

    def validate_model(self, model: JujuModelHandle, level: str) -> None:
        self.calls.append(("validate", model, level))


def test_missing_mapping_skips_before_controller_mutation() -> None:
    # GIVEN a controller on a different cloud from its workload
    backend = CloudBackend({"target:controller": "controller-cloud", TARGET.uri: "workload-cloud"})
    client = RecordingClient()
    registry = KubernetesClientRegistry({"workload-cloud": Path("workload.yaml")})

    # WHEN restarting, THEN the missing controller mapping skips before any mutation
    with pytest.raises(pytest.skip.Exception, match="Set KUBECONFIG_controller_cloud"):
        restart.test_controller_restart(cast(JujuClient, client), TARGET, None, backend, registry)
    assert client.calls == []
    assert backend.resolved == [JujuModelHandle(controller="target", model="controller")]


@pytest.mark.parametrize("cloud", [None, "controller-cloud"], ids=["machine", "kubernetes"])
def test_restart_preserves_idle_and_both_model_validations(cloud: str | None) -> None:
    # GIVEN either a machine controller or a configured Kubernetes controller
    backend = CloudBackend({"target:controller": cloud})
    registry = KubernetesClientRegistry({cloud: Path("controller.yaml")} if cloud is not None else {})
    client = RecordingClient()

    # WHEN restarting the controller
    restart.test_controller_restart(cast(JujuClient, client), TARGET, NEIGHBOR, backend, registry)

    # THEN recovery and endpoint validation still follow the restart
    assert client.calls == [
        ("restart", TARGET),
        ("idle", [TARGET, NEIGHBOR], timedelta(minutes=15)),
        ("validate", TARGET, "deep"),
        ("validate", NEIGHBOR, "deep"),
    ]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_configured_restart_errors_propagate(status: int) -> None:
    # GIVEN configured access and an operation that fails
    backend = CloudBackend({"target:controller": "controller-cloud"})
    registry = KubernetesClientRegistry({"controller-cloud": Path("controller.yaml")})
    error = ApiException(status=status)
    client = RecordingClient(error)

    # WHEN restarting, THEN the original failure is not converted to a skip
    with pytest.raises(ApiException) as exc_info:
        restart.test_controller_restart(cast(JujuClient, client), TARGET, None, backend, registry)
    assert exc_info.value is error
    assert client.calls == [("restart", TARGET)]


@dataclass(frozen=True)
class MigrationParams:
    label: str
    configured_cloud: str
    missing_cloud: str


@pytest.mark.parametrize(
    "params",
    [
        MigrationParams("missing-source", "destination-cloud", "source-cloud"),
        MigrationParams("missing-destination", "source-cloud", "destination-cloud"),
    ],
    ids=lambda params: params.label,
)
def test_migration_checks_both_controllers_before_moving_model(params: MigrationParams) -> None:
    # GIVEN one Kubernetes controller without a kubeconfig
    backend = CloudBackend({"target:controller": "source-cloud", "temporary:controller": "destination-cloud"})
    registry = KubernetesClientRegistry({params.configured_cloud: Path("configured.yaml")})
    client = RecordingClient()

    # WHEN preparing migration, THEN it skips before validation or migration can mutate state
    with pytest.raises(pytest.skip.Exception, match=f"KUBECONFIG_{params.missing_cloud.replace('-', '_')}"):
        migration.test_model_controller_migration(
            cast(JujuClient, client), backend, "target", "temporary", "workload", TARGET, None, registry
        )
    assert client.calls == []


def test_target_kubernetes_client_skips_missing_mapping() -> None:
    # GIVEN a Kubernetes workload without a mapping
    backend = CloudBackend({TARGET.uri: "workload-cloud"})

    # WHEN resolving the fixture, THEN it skips with the exact variable name
    with pytest.raises(pytest.skip.Exception, match="Set KUBECONFIG_workload_cloud"):
        unwrap(conftest.kubernetes_client)(KubernetesClientRegistry({}), TARGET, backend)


def test_target_machine_client_does_not_require_kubeconfig() -> None:
    # GIVEN a machine workload
    backend = CloudBackend({TARGET.uri: None})

    # WHEN resolving the fixture, THEN existing non-Kubernetes behavior is preserved
    assert unwrap(conftest.kubernetes_client)(KubernetesClientRegistry({}), TARGET, backend) is None


def test_configured_missing_file_fails_instead_of_skipping(tmp_path: Path) -> None:
    # GIVEN an explicit mapping to a nonexistent file
    backend = CloudBackend({TARGET.uri: "workload-cloud"})
    registry = KubernetesClientRegistry({"workload-cloud": tmp_path / "missing.yaml"})

    # WHEN constructing the client, THEN a configuration error is not converted to a skip
    with pytest.raises(ConfigException):
        unwrap(conftest.kubernetes_client)(registry, TARGET, backend)
