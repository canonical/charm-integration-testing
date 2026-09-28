# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905: remove with the diagnostic helper after investigation."""

from unittest.mock import MagicMock

from kubernetes_client.cpu_stress_diagnostics import log_cpu_stress_snapshot


def test_unavailable_metrics_and_pod_do_not_prevent_litmus_collection() -> None:
    backend = MagicMock()
    backend.core_v1_api.read_namespaced_pod.side_effect = TimeoutError()
    custom = backend.custom_objects_api
    custom.get_namespaced_custom_object.side_effect = PermissionError()
    custom.list_namespaced_custom_object.return_value = {"items": []}

    log_cpu_stress_snapshot(backend, "model", "target-0", "before cleanup")

    custom.list_namespaced_custom_object.assert_called_once()
    assert custom.list_namespaced_custom_object.call_args.kwargs["plural"] == "chaosengines"


def test_only_matching_engine_children_are_collected() -> None:
    backend = MagicMock()
    custom = backend.custom_objects_api
    custom.list_namespaced_custom_object.side_effect = [
        {
            "items": [
                {
                    "metadata": {"uid": "unrelated"},
                    "spec": {"selectors": {"pods": [{"namespace": "model", "names": "other-0"}]}},
                },
                {
                    "metadata": {"uid": "selected"},
                    "spec": {"selectors": {"pods": [{"namespace": "model", "names": "target-0"}]}},
                },
            ]
        },
        {"items": []},
    ]
    backend.core_v1_api.list_namespaced_pod.return_value.items = []

    log_cpu_stress_snapshot(backend, "model", "target-0", "before cleanup")

    assert custom.list_namespaced_custom_object.call_count == 2
    assert custom.list_namespaced_custom_object.call_args.kwargs["label_selector"] == "chaosUID=selected"
    assert backend.core_v1_api.list_namespaced_pod.call_args.kwargs["label_selector"] == "chaosUID=selected"
