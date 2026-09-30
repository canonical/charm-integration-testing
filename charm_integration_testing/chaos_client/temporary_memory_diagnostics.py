# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Temporary SQT-904 evidence capture; remove with diagnostic hooks and tests.

Memory experiments use jobCleanUpPolicy=retain while this instrumentation is
installed. Litmus 3.31.0 pkg/utils/common/pods.go honours that setting for helper
Pods. Our normal UID-checked cleanup still deletes the resources after capture.
Evidence is printed to the test log only; no diagnostic files are written.
"""

from __future__ import annotations

import json
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .litmus_client import LitmusChaosClient, _ExperimentRun

REQUEST_TIMEOUT = (3, 5)


def diagnose_cleanup(client: LitmusChaosClient, engine: _ExperimentRun, stage: str) -> None:
    """Best-effort snapshot with bounded API calls, without changing experiment state."""
    if not engine.diagnose_memory or engine.uid is None:
        return
    periodic = stage in {"startup", "observation"}
    now = monotonic()
    if periodic and engine.diagnostic_last_sample is not None and now - engine.diagnostic_last_sample < 10:
        return
    engine.diagnostic_last_sample = now
    logger = logging.getLogger(__name__)

    def emit(label: str, value: Any) -> None:
        # Split text and JSON to avoid losing the useful tail in Actions' UI.
        rendered = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        for line in rendered.splitlines():
            for offset in range(0, max(1, len(line)), 900):
                logger.info("SQT-904 DIAGNOSTIC [%s] %s %s: %s", stage, engine.name, label, line[offset : offset + 900])

    def collect(label: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as error:
            emit(label + " unavailable", str(error))
            return None

    def snapshot_pod(pod: Any) -> None:
        metadata, status = pod.metadata, pod.status
        emit(
            "Pod " + metadata.name,
            {
                "uid": metadata.uid,
                "deletion_timestamp": metadata.deletion_timestamp,
                "node": pod.spec.node_name,
                "phase": status.phase if status else None,
                "reason": status.reason if status else None,
                "message": status.message if status else None,
            },
        )
        if status:
            for item in (status.init_container_statuses or []) + (status.container_statuses or []):
                emit(f"container {metadata.name}/{item.name}", item.to_dict())

    core = client._backend.core_v1_api
    try:
        current = collect("ChaosEngine", lambda: client._read_engine(engine))
        emit(
            "ChaosEngine",
            None
            if current is None
            else {
                "uid": current["metadata"].get("uid"),
                "status": current.get("status"),
                "engineState": current.get("spec", {}).get("engineState"),
                "jobCleanUpPolicy": current.get("spec", {}).get("jobCleanUpPolicy"),
            },
        )
        results = collect("ChaosResults", lambda: client._results(engine))
        for result in results or []:
            emit(
                "ChaosResult " + result.get("metadata", {}).get("name", "unknown"),
                {
                    "target_annotation": (result.get("metadata", {}).get("annotations") or {}).get(f"pod/{engine.pod}"),
                    "status": result.get("status"),
                },
            )
        target = collect(
            "target Pod",
            lambda: core.read_namespaced_pod(
                name=engine.pod,
                namespace=engine.namespace,
                _request_timeout=REQUEST_TIMEOUT,
            ),
        )
        if target is not None:
            snapshot_pod(target)
        # Helpers need not have chaosUID. Their run-specific app label and service
        # account let us discover them before an error names them in ChaosResult.
        pods = collect(
            "experiment Pods",
            lambda: core.list_namespaced_pod(
                namespace=engine.namespace,
                _request_timeout=REQUEST_TIMEOUT,
            ),
        )
        selected = {}
        for pod in pods.items if pods is not None else []:
            labels = pod.metadata.labels or {}
            if labels.get("chaosUID") == engine.uid or (
                pod.spec.service_account_name == engine.name
                and (
                    labels.get("app", "").startswith(engine.name + "-helper-")
                    or pod.metadata.name.startswith(engine.name + "-")
                )
            ):
                selected[pod.metadata.name] = pod
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
        for name in sorted(names)[:12]:
            if name not in selected:
                pod = collect(
                    "named experiment Pod " + name,
                    lambda: core.read_namespaced_pod(
                        name=name,
                        namespace=engine.namespace,
                        _request_timeout=REQUEST_TIMEOUT,
                    ),
                )
                if pod is not None:
                    selected[name] = pod
        for pod in list(selected.values())[:12]:
            snapshot_pod(pod)
            states = (pod.status.container_statuses or []) if pod.status else []
            states += (pod.status.init_container_statuses or []) if pod.status else []
            for container in (pod.spec.init_containers or []) + pod.spec.containers:
                restarted = any(item.name == container.name and item.restart_count > 0 for item in states)
                for previous in [False, True] if restarted else [False]:
                    label = f"helper log {pod.metadata.name}/{container.name} previous={previous}"
                    content = collect(
                        label,
                        lambda: core.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=engine.namespace,
                            container=container.name,
                            previous=previous,
                            tail_lines=300,
                            limit_bytes=65536,
                            timestamps=True,
                            _request_timeout=REQUEST_TIMEOUT,
                        ),
                    )
                    if content is not None:
                        emit(label, content)
        relevant = {engine.name, engine.pod, *names, *selected}
        events = collect(
            "events",
            lambda: core.list_namespaced_event(
                namespace=engine.namespace,
                _request_timeout=REQUEST_TIMEOUT,
            ),
        )
        for event in (events.items if events is not None else [])[-200:]:
            if event.involved_object.name in relevant:
                emit("event", event.to_dict())
        nodes = {pod.spec.node_name for pod in selected.values() if pod.spec.node_name}
        if target is not None and target.spec.node_name:
            nodes.add(target.spec.node_name)
        for node_name in sorted(nodes)[:3]:
            node = collect("node " + node_name, lambda: core.read_node(node_name, _request_timeout=REQUEST_TIMEOUT))
            if node is not None and node.status:
                emit(
                    "node " + node_name,
                    {
                        "conditions": [item.to_dict() for item in node.status.conditions or []],
                        "capacity": node.status.capacity,
                        "allocatable": node.status.allocatable,
                    },
                )
            node_events = collect(
                "node events " + node_name,
                lambda: core.list_event_for_all_namespaces(
                    field_selector=f"involvedObject.kind=Node,involvedObject.name={node_name}",
                    _request_timeout=REQUEST_TIMEOUT,
                ),
            )
            for event in (node_events.items if node_events is not None else [])[-30:]:
                emit("node event", event.to_dict())
    except Exception as error:
        # Diagnostics must never replace injection or cleanup failures.
        emit("snapshot incomplete", str(error))
