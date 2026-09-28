# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""TEMPORARY SQT-905 diagnostics: remove after the live failure is understood."""

import logging
from typing import Any, Callable

from .backend import KubernetesBackend


def log_cpu_stress_snapshot(backend: KubernetesBackend, namespace: str, pod_name: str, phase: str) -> None:
    """Read diagnostics without allowing collection failures to interrupt cleanup."""
    logger = logging.getLogger(__name__)

    def collect(label: str, read: Callable[[], Any]) -> Any:
        try:
            value = read()
            logger.info("SQT-905 DIAGNOSTIC [%s] %s: %.12000s", phase, label, value)
            return value
        except Exception as error:
            logger.warning("SQT-905 DIAGNOSTIC [%s] %s unavailable (%s)", phase, label, type(error).__name__)
            return None

    core = backend.core_v1_api
    custom = backend.custom_objects_api

    def target() -> dict[str, Any]:
        pod = core.read_namespaced_pod(name=pod_name, namespace=namespace, _request_timeout=5)
        return {
            "name": pod.metadata.name,
            "uid": pod.metadata.uid,
            "status": pod.status.to_dict() if pod.status else None,
            "resources": {c.name: c.resources.to_dict() if c.resources else None for c in pod.spec.containers},
        }

    collect("target Pod", target)
    collect(
        "CPU metrics (timestamp/window apply; unavailable does not mean zero usage)",
        lambda: custom.get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            name=pod_name,
            _request_timeout=5,
        ),
    )

    def litmus() -> None:
        engines = custom.list_namespaced_custom_object(
            group="litmuschaos.io",
            version="v1alpha1",
            namespace=namespace,
            plural="chaosengines",
            limit=20,
            _request_timeout=5,
        )
        for engine in engines.get("items", [])[:20]:
            selectors = engine.get("spec", {}).get("selectors", {}).get("pods", [])
            if not any(
                s.get("namespace") == namespace and pod_name in s.get("names", "").split(",") for s in selectors
            ):
                continue
            metadata = engine.get("metadata", {})
            collect(
                "ChaosEngine",
                lambda: {
                    "name": metadata.get("name"),
                    "uid": metadata.get("uid"),
                    "spec": engine.get("spec"),
                    "status": engine.get("status"),
                },
            )
            if not metadata.get("uid"):
                continue
            selector = f"chaosUID={metadata['uid']}"
            collect(
                "ChaosResults",
                lambda: custom.list_namespaced_custom_object(
                    group="litmuschaos.io",
                    version="v1alpha1",
                    namespace=namespace,
                    plural="chaosresults",
                    label_selector=selector,
                    limit=10,
                    _request_timeout=5,
                ),
            )
            pods = core.list_namespaced_pod(namespace=namespace, label_selector=selector, limit=5, _request_timeout=5)
            for pod in pods.items[:5]:
                collect("helper Pod status", lambda: {"name": pod.metadata.name, "status": pod.status.to_dict()})
                for container in (pod.spec.init_containers or []) + pod.spec.containers:
                    collect(
                        f"helper log {pod.metadata.name}/{container.name}",
                        lambda: core.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=namespace,
                            container=container.name,
                            tail_lines=30,
                            limit_bytes=4000,
                            _request_timeout=5,
                        ),
                    )

    collect("Litmus collection", litmus)
