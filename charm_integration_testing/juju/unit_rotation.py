# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import TYPE_CHECKING

from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient

from .backend import JujuBackend
from .handles import JujuModelHandle

if TYPE_CHECKING:
    from .client import JujuClient


def rotate_application_units(
    juju_client: "JujuClient",
    backend: JujuBackend,
    application: str,
    model: JujuModelHandle,
    *,
    related_models: list[JujuModelHandle] | None = None,
    timeout: timedelta = timedelta(minutes=15),
) -> None:
    units = juju_client.application_units(application, model=model)
    if not units:
        raise ValueError(f"Cannot rotate units for application {application}: it has no units.")
    models = list(dict.fromkeys([model, *(related_models or [])]))
    if backend.is_k8s_model(model):
        kubernetes_client = backend.get_kubernetes_client_for_model(model)
        if kubernetes_client is None:
            raise RuntimeError(f"No KubernetesClient available for Kubernetes model {model.uri}.")
        _rotate_kubernetes_units(juju_client, kubernetes_client, application, model, units, models, timeout)
    else:
        _rotate_machine_units(juju_client, application, model, units, models, timeout)

    current_units = juju_client.application_units(application, model=model)
    if len(current_units) != len(units):
        raise RuntimeError(f"Expected {len(units)} units after rotation, found {len(current_units)}.")


def _rotate_kubernetes_units(
    juju_client: "JujuClient",
    kubernetes_client: KubernetesClient,
    application: str,
    model: JujuModelHandle,
    units: list[str],
    models: list[JujuModelHandle],
    timeout: timedelta,
) -> None:
    pods = kubernetes_client.get_charm_pods(application, model=model.model)
    if len(pods) != len(units):
        raise RuntimeError(
            f"Expected one workload pod per Juju unit for {application}, found {len(pods)} pods for {len(units)} units."
        )
    original_uids = {pod.metadata.uid for pod in pods if pod.metadata is not None and pod.metadata.uid is not None}
    try:
        juju_client.scale_application(application, len(units) + 1, model=model)
        kubernetes_client.wait_for_charm_pods_ready(
            application,
            model.model,
            expected_count=len(units) + 1,
            timeout=timeout,
        )
        juju_client.multi_model_idle_for_period(models, timeout=timeout)
        for unit, pod in zip(units, pods, strict=True):
            if pod.metadata is None or pod.metadata.name is None or pod.metadata.uid is None:
                raise RuntimeError(f"Kubernetes pod metadata is incomplete for unit {unit}.")
            current_pods = kubernetes_client.get_charm_pods(application, model=model.model)
            existing_uids = {
                current_pod.metadata.uid
                for current_pod in current_pods
                if current_pod.metadata is not None and current_pod.metadata.uid is not None
            }
            if pod.metadata.uid not in existing_uids:
                raise RuntimeError(f"Kubernetes pod for unit {unit} disappeared before its rotation.")

            juju_client.delete_workload_pod(model=model, pod_name=pod.metadata.name)
            replacement_pod = kubernetes_client.wait_for_new_pod(
                application_name=application,
                namespace=model.model,
                existing_uids=existing_uids,
                timeout=timeout,
            )
            if (
                replacement_pod.metadata is None
                or replacement_pod.metadata.name is None
                or replacement_pod.metadata.uid is None
            ):
                raise RuntimeError(f"Replacement pod metadata is incomplete for unit {unit}.")
            kubernetes_client.wait_for_pod_ready(
                pod_name=replacement_pod.metadata.name,
                namespace=model.model,
                timeout=timeout,
            )
            juju_client.multi_model_idle_for_period(models, timeout=timeout)
            for model_ref in models:
                juju_client.validate_model(model=model_ref, level="simple")
    except BaseException as rotation_error:
        try:
            _restore_kubernetes_unit_count(
                juju_client, kubernetes_client, application, model, len(units), models, timeout
            )
        except BaseException as cleanup_error:
            raise rotation_error from cleanup_error
        raise
    else:
        _restore_kubernetes_unit_count(juju_client, kubernetes_client, application, model, len(units), models, timeout)

    _wait_for_rotated_kubernetes_pods(kubernetes_client, application, model.model, len(units), original_uids, timeout)
    for model_ref in models:
        juju_client.validate_model(model=model_ref, level="simple")


def _restore_kubernetes_unit_count(
    juju_client: "JujuClient",
    kubernetes_client: KubernetesClient,
    application: str,
    model: JujuModelHandle,
    unit_count: int,
    models: list[JujuModelHandle],
    timeout: timedelta,
) -> None:
    cleanup_errors: list[Exception] = []
    try:
        juju_client.scale_application(application, unit_count, model=model)
    except Exception as error:
        cleanup_errors.append(error)
    try:
        juju_client.multi_model_idle_for_period(models, timeout=timeout)
    except Exception as error:
        cleanup_errors.append(error)
    try:
        kubernetes_client.wait_for_charm_pods_ready(
            application,
            model.model,
            expected_count=unit_count,
            timeout=timeout,
        )
    except Exception as convergence_error:
        cleanup_errors.append(convergence_error)

    _raise_cleanup_errors(cleanup_errors)


def _wait_for_rotated_kubernetes_pods(
    kubernetes_client: KubernetesClient,
    application: str,
    namespace: str,
    expected_count: int,
    original_uids: set[str],
    timeout: timedelta,
) -> None:
    def expected_pods() -> list[K8sClient.V1Pod] | None:
        observed_pods = kubernetes_client.get_charm_pods(application, model=namespace)
        observed_uids = {
            pod.metadata.uid for pod in observed_pods if pod.metadata is not None and pod.metadata.uid is not None
        }
        if len(observed_pods) == expected_count and not original_uids & observed_uids:
            return observed_pods
        return None

    kubernetes_client.wait(
        check=expected_pods,
        timeout_message=f"Kubernetes pods for {application} did not converge to {expected_count} rotated pods within timeout.",
        timeout=timeout,
    )


def _rotate_machine_units(
    juju_client: "JujuClient",
    application: str,
    model: JujuModelHandle,
    units: list[str],
    models: list[JujuModelHandle],
    timeout: timedelta,
) -> None:
    for unit in units:
        original_units = set(juju_client.application_units(application, model=model))
        try:
            juju_client.scale_application(application, len(units) + 1, model=model)
            juju_client.multi_model_idle_for_period(models, timeout=timeout)
            juju_client.remove_unit(unit, model=model)
            juju_client.multi_model_idle_for_period(models, timeout=timeout)
            current_units = juju_client.application_units(application, model=model)
            if len(current_units) != len(units):
                raise RuntimeError(f"Expected {len(units)} units after rotating {unit}, found {len(current_units)}.")
            if unit in current_units:
                raise RuntimeError(f"Unit {unit} was not removed after rotation.")
            for model_ref in models:
                juju_client.validate_model(model=model_ref, level="simple")
        except BaseException as rotation_error:
            try:
                _cleanup_machine_surge(
                    juju_client, application, model, original_units, len(units), models, unit, timeout
                )
            except BaseException as cleanup_error:
                raise rotation_error from cleanup_error
            raise


def _cleanup_machine_surge(
    juju_client: "JujuClient",
    application: str,
    model: JujuModelHandle,
    original_units: set[str],
    unit_count: int,
    models: list[JujuModelHandle],
    rotated_unit: str,
    timeout: timedelta,
) -> None:
    current_units = juju_client.application_units(application, model=model)
    cleanup_errors: list[Exception] = []
    if set(current_units) != original_units:
        try:
            juju_client.multi_model_idle_for_period(models, timeout=timeout)
        except Exception as error:
            cleanup_errors.append(error)
        try:
            current_units = juju_client.application_units(application, model=model)
        except Exception as error:
            cleanup_errors.append(error)
            _raise_cleanup_errors(cleanup_errors)

    surge_units = [current_unit for current_unit in current_units if current_unit not in original_units]
    cleanup_changed_state = False
    if len(current_units) > unit_count and not surge_units:
        cleanup_errors.append(RuntimeError(f"Unable to identify a surge unit to remove after rotating {rotated_unit}."))
    elif surge_units:
        surge_unit_count = max(0, len(current_units) - unit_count)
        for surge_unit in surge_units[:surge_unit_count]:
            cleanup_changed_state = True
            try:
                juju_client.remove_unit(surge_unit, model=model)
            except Exception as error:
                cleanup_errors.append(error)
    if cleanup_changed_state or set(current_units) != original_units:
        try:
            juju_client.multi_model_idle_for_period(models, timeout=timeout)
        except Exception as error:
            cleanup_errors.append(error)
    _raise_cleanup_errors(cleanup_errors)


def _raise_cleanup_errors(errors: list[Exception]) -> None:
    if errors:
        if len(errors) > 1:
            raise errors[0] from errors[-1]
        raise errors[0]
