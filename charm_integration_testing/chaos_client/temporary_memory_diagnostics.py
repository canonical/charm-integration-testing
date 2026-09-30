# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Temporary SQT-904 diagnostics. Remove this module and its call site after diagnosis."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .litmus_client import LitmusChaosClient, _ExperimentRun


def diagnose_cleanup(client: LitmusChaosClient, engine: _ExperimentRun, stage: str) -> None:
    # Temporary SQT-904 diagnostics; remove after investigating OOM cleanup.
    if not engine.diagnose_memory or engine.uid is None:
        return
    logger = logging.getLogger(__name__)

    def collect(label: str, read: Callable[[], Any]) -> Any:
        try:
            value = read()
            summary = value
            if label.startswith("named experiment Pod"):
                summary = {"name": value.metadata.name, "status": value.status.to_dict() if value.status else None}
            if label == "helper Pod statuses":
                summary = [
                    {
                        "name": pod.metadata.name,
                        "uid": pod.metadata.uid,
                        "status": pod.status.to_dict() if pod.status else None,
                    }
                    for pod in value.items
                ]
            logger.info("SQT-904 DIAGNOSTIC [%s] engine=%s %s: %s", stage, engine.name, label, summary)
            return value
        except Exception as error:
            logger.warning("SQT-904 DIAGNOSTIC [%s] %s unavailable: %s", stage, label, error)
            return None

    def engine_status() -> Any:
        current = client._read_engine(engine)
        return None if current is None else {"uid": current["metadata"].get("uid"), "status": current.get("status")}

    current = collect("ChaosEngine", engine_status)
    results = collect(
        "ChaosResults",
        lambda: [
            {
                "name": result.get("metadata", {}).get("name"),
                "target_annotation": (result.get("metadata", {}).get("annotations") or {}).get(f"pod/{engine.pod}"),
                "status": result.get("status"),
            }
            for result in client._results(engine)
        ],
    )
    core = client._backend.core_v1_api

    def target_status() -> Any:
        pod = core.read_namespaced_pod(name=engine.pod, namespace=engine.namespace, _request_timeout=(3, 5))
        return {"uid": pod.metadata.uid, "status": pod.status.to_dict() if pod.status else None}

    collect("target Pod", target_status)
    pods = collect(
        "helper Pod statuses",
        lambda: core.list_namespaced_pod(
            namespace=engine.namespace, label_selector=f"chaosUID={engine.uid}", _request_timeout=(3, 5)
        ),
    )
    selected = {pod.metadata.name: pod for pod in pods.items} if pods is not None else {}
    names: set[str] = set()
    for experiment in (current or {}).get("status", {}).get("experiments", []):
        names.update(experiment.get(key) for key in ("experimentPod", "runner") if experiment.get(key))
    for result in results or []:
        error = (result.get("status") or {}).get("experimentStatus", {}).get("errorOutput") or {}
        try:
            source = json.loads(error.get("reason", "{}")).get("source")
            if source:
                names.add(source)
        except (ValueError, TypeError, AttributeError):
            pass
    for name in sorted(names)[:8]:
        if name not in selected:
            pod = collect(
                f"named experiment Pod {name}",
                lambda: core.read_namespaced_pod(name=name, namespace=engine.namespace, _request_timeout=(3, 5)),
            )
            if pod is not None:
                selected[name] = pod
    for pod in list(selected.values())[:8]:
        for container in (pod.spec.init_containers or []) + pod.spec.containers:
            collect(
                f"helper log {pod.metadata.name}/{container.name}",
                lambda: core.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=engine.namespace,
                    container=container.name,
                    tail_lines=80,
                    limit_bytes=8192,
                    timestamps=True,
                    _request_timeout=(3, 5),
                ),
            )
