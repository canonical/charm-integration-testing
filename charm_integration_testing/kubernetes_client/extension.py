# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC


class KubernetesExtension(ABC):
    def post_delete_pod(self, namespace: str, pod_name: str) -> None:
        pass

    def post_restart_statefulset(self, namespace: str, statefulset_name: str) -> None:
        pass
