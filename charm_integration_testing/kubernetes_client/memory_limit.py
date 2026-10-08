# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from typing import Iterator

from kubernetes.utils.quantity import parse_quantity  # type: ignore[import-untyped]

from .client import KubernetesClient
from .resource_limit import temporary_resource_limit


@contextmanager
def temporary_memory_limit(
    kubernetes: KubernetesClient, namespace: str, name: str, uid: str, container: str, timeout: int, limit: str
) -> Iterator[None]:
    """Limit one StatefulSet workload container to a memory limit, then restore its memory resources."""
    if parse_quantity(limit) <= 0:
        raise ValueError("Memory limit must be positive.")
    with temporary_resource_limit(
        kubernetes,
        namespace,
        name,
        uid,
        container,
        timeout,
        resource="memory",
        limit=limit,
        default_request="128Mi",
    ):
        yield
