# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuBackend, JujuClient, JujuModelHandle
from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient

from bundle_builder_x import Charm

from .scheduler.states import State

_UNIT_ROTATION_TIMEOUT = timedelta(minutes=15)


def _require_principal_charm(charm: Charm | None) -> Charm:
    if charm is None:
        pytest.fail("Unable to resolve the deployed target charm metadata needed for HA tests.")
    if charm.subordinate:
        pytest.skip(f"{charm.name} is subordinate and cannot be scaled independently.")
    return charm


def _bundle_application_units(bundle_path: Path, application: str, platform: str) -> int:
    with bundle_path.open(encoding="utf-8") as file:
        try:
            bundle = next(yaml.safe_load_all(file))
        except StopIteration:
            raise ValueError(f"Bundle is empty: {bundle_path}") from None

    if not isinstance(bundle, dict):
        raise ValueError(f"Invalid bundle document in {bundle_path}.")
    applications = bundle.get("applications")
    if not isinstance(applications, dict) or application not in applications:
        raise ValueError(f"Application '{application}' not found in bundle: {bundle_path}")
    application_data = applications[application]
    if not isinstance(application_data, dict):
        raise ValueError(f"Invalid application definition for '{application}' in {bundle_path}.")

    units_key = "scale" if platform == "kubernetes" else "num_units"
    units = application_data.get(units_key)
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"Application '{application}' in {bundle_path} must define a positive integer '{units_key}'.")
    return units


def _models_to_validate(
    target_model_ref: JujuModelHandle, neighbor_model_ref: JujuModelHandle | None
) -> list[JujuModelHandle]:
    return [model for model in (target_model_ref, neighbor_model_ref) if model is not None]


def _wait_for_rotated_kubernetes_pods(
    kubernetes_client: KubernetesClient,
    target_application: str,
    namespace: str,
    expected_count: int,
    original_uids: set[str],
) -> None:
    def expected_pods() -> list[K8sClient.V1Pod] | None:
        observed_pods = kubernetes_client.get_charm_pods(target_application, model=namespace)
        observed_uids = {
            pod.metadata.uid for pod in observed_pods if pod.metadata is not None and pod.metadata.uid is not None
        }
        if len(observed_pods) == expected_count and not original_uids & observed_uids:
            return observed_pods
        return None

    kubernetes_client.wait(
        check=expected_pods,
        timeout_message=(
            f"Kubernetes pods for {target_application} did not converge to {expected_count} rotated pods "
            "within timeout."
        ),
        timeout=_UNIT_ROTATION_TIMEOUT,
    )


def _restore_kubernetes_pod_count(
    juju_client: JujuClient,
    kubernetes_client: KubernetesClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    unit_count: int,
    models: list[JujuModelHandle],
) -> None:
    cleanup_errors: list[Exception] = []
    try:
        juju_client.scale_application(target_application, unit_count, model=target_model_ref)
    except Exception as error:
        cleanup_errors.append(error)
    try:
        juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
    except Exception as error:
        cleanup_errors.append(error)
    try:
        kubernetes_client.wait_for_charm_pods_ready(
            target_application,
            target_model_ref.model,
            expected_count=unit_count,
            timeout=_UNIT_ROTATION_TIMEOUT,
        )
    except Exception as convergence_error:
        cleanup_errors.append(convergence_error)

    if cleanup_errors:
        if len(cleanup_errors) > 1:
            raise cleanup_errors[0] from cleanup_errors[-1]
        raise cleanup_errors[0]


def _rotate_kubernetes_pods(
    juju_client: JujuClient,
    kubernetes_client: KubernetesClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    units: list[str],
    pods: list[K8sClient.V1Pod],
    models: list[JujuModelHandle],
) -> None:
    namespace = target_model_ref.model
    original_uids = {pod.metadata.uid for pod in pods if pod.metadata is not None and pod.metadata.uid is not None}
    try:
        juju_client.scale_application(target_application, len(units) + 1, model=target_model_ref)
        juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
        for unit, pod in zip(units, pods, strict=True):
            if pod.metadata is None or pod.metadata.name is None or pod.metadata.uid is None:
                pytest.fail(f"Kubernetes pod metadata is incomplete for unit {unit}.")
            current_pods = kubernetes_client.get_charm_pods(target_application, model=namespace)
            existing_uids = {
                current_pod.metadata.uid
                for current_pod in current_pods
                if current_pod.metadata is not None and current_pod.metadata.uid is not None
            }
            if pod.metadata.uid not in existing_uids:
                pytest.fail(f"Kubernetes pod for unit {unit} disappeared before its rotation.")

            kubernetes_client.delete_pod(namespace=namespace, pod_name=pod.metadata.name)
            replacement_pod = kubernetes_client.wait_for_new_pod(
                application_name=target_application,
                namespace=namespace,
                existing_uids=existing_uids,
                timeout=_UNIT_ROTATION_TIMEOUT,
            )
            if (
                replacement_pod.metadata is None
                or replacement_pod.metadata.name is None
                or replacement_pod.metadata.uid is None
            ):
                pytest.fail(f"Replacement pod metadata is incomplete for unit {unit}.")
            kubernetes_client.wait_for_pod_ready(
                pod_name=replacement_pod.metadata.name,
                namespace=namespace,
                timeout=_UNIT_ROTATION_TIMEOUT,
            )
            juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
            for model in models:
                juju_client.validate_model(model=model, level="simple")
    finally:
        _restore_kubernetes_pod_count(
            juju_client,
            kubernetes_client,
            target_model_ref,
            target_application,
            len(units),
            models,
        )

    _wait_for_rotated_kubernetes_pods(kubernetes_client, target_application, namespace, len(units), original_uids)
    for model in models:
        juju_client.validate_model(model=model, level="simple")


def _cleanup_machine_surge(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    original_units: set[str],
    unit_count: int,
    models: list[JujuModelHandle],
    rotated_unit: str,
) -> None:
    current_units = juju_client.application_units(target_application, model=target_model_ref)
    if len(current_units) > unit_count:
        surge_units = [current_unit for current_unit in current_units if current_unit not in original_units]
        cleanup_errors: list[Exception] = []
        if not surge_units:
            cleanup_errors.append(
                RuntimeError(f"Unable to identify a surge unit to remove after rotating {rotated_unit}.")
            )
        else:
            for surge_unit in surge_units[: len(current_units) - unit_count]:
                try:
                    juju_client.remove_unit(surge_unit, model=target_model_ref)
                except Exception as error:
                    cleanup_errors.append(error)
        try:
            juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
        except Exception as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if len(cleanup_errors) > 1:
                raise cleanup_errors[0] from cleanup_errors[-1]
            raise cleanup_errors[0]


def _rotate_machine_units(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    units: list[str],
    models: list[JujuModelHandle],
) -> None:
    for unit in units:
        original_units = set(juju_client.application_units(target_application, model=target_model_ref))
        try:
            juju_client.scale_application(target_application, len(units) + 1, model=target_model_ref)
            juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
            juju_client.remove_unit(unit, model=target_model_ref)
            juju_client.multi_model_idle_for_period(models, timeout=_UNIT_ROTATION_TIMEOUT)
            current_units = juju_client.application_units(target_application, model=target_model_ref)
            if len(current_units) != len(units):
                pytest.fail(f"Expected {len(units)} units after rotating {unit}, found {len(current_units)}.")
            if unit in current_units:
                pytest.fail(f"Unit {unit} was not removed after rotation.")
            for model in models:
                juju_client.validate_model(model=model, level="simple")
        finally:
            _cleanup_machine_surge(
                juju_client,
                target_model_ref,
                target_application,
                original_units,
                len(units),
                models,
                unit,
            )


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED_HA)
def test_scale_to_ha(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = _require_principal_charm(target_deployed_charm)
    ha_units = charm.ha_units
    current_units = juju_client.num_units(target_application, model=target_model_ref)
    if current_units < ha_units:
        juju_client.scale_application(target_application, ha_units, model=target_model_ref)
        juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))

    juju_client.validate_model(model=target_model_ref, level="deep")


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED)
def test_scale_from_ha(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_bundle: Path,
    target_application: str,
    target_platform: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = _require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")

    original_units = _bundle_application_units(target_bundle, target_application, target_platform)
    juju_client.scale_application(target_application, original_units, model=target_model_ref)
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15))
    juju_client.validate_model(model=target_model_ref, level="simple")


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_unit_rotation(
    juju_client: JujuClient,
    juju_backend: JujuBackend,
    kubernetes_client: KubernetesClient | None,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_deployed_charm: Charm | None,
) -> None:
    charm = _require_principal_charm(target_deployed_charm)
    if not charm.scale_down:
        pytest.skip(f"{charm.name} does not support scaling down from HA.")
    units = juju_client.application_units(target_application, model=target_model_ref)
    if len(units) < charm.ha_units:
        pytest.fail(
            f"Application {target_application} has {len(units)} units, fewer than its HA requirement "
            f"of {charm.ha_units}."
        )

    is_k8s_model = juju_backend.is_k8s_model(target_model_ref)
    pods: list[K8sClient.V1Pod] = []
    if is_k8s_model:
        if kubernetes_client is None:
            pytest.fail("KubernetesClient was not instantiated correctly. Is KUBECONFIG set?")
        pods = kubernetes_client.get_charm_pods(target_application, model=target_model_ref.model)
        if len(pods) != len(units):
            pytest.fail(
                f"Expected one workload pod per Juju unit for {target_application}, "
                f"found {len(pods)} pods for {len(units)} units."
            )

    models = _models_to_validate(target_model_ref, neighbor_model_ref)
    if is_k8s_model:
        assert kubernetes_client is not None
        _rotate_kubernetes_pods(
            juju_client, kubernetes_client, target_model_ref, target_application, units, pods, models
        )
    else:
        _rotate_machine_units(juju_client, target_model_ref, target_application, units, models)

    current_units = juju_client.application_units(target_application, model=target_model_ref)
    if len(current_units) != len(units):
        pytest.fail(f"Expected {len(units)} units after rotation, found {len(current_units)}.")
