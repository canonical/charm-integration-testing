# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import contextmanager
from typing import Iterator

from .client import KubernetesClient
from .resource_limit import temporary_resource_limit


@contextmanager
def temporary_cpu_limit(
    kubernetes: KubernetesClient, namespace: str, name: str, uid: str, container: str, timeout: int
) -> Iterator[None]:
    """Limit one StatefulSet workload container to one CPU, then restore its CPU resources."""
    with temporary_resource_limit(
        kubernetes,
        namespace,
        name,
        uid,
        container,
        timeout,
        resource="cpu",
        limit="1",
        default_request="100m",
    ):
        yield
