# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes.client import ApiException  # type: ignore[import-untyped]


class FakeCustomObjectsApi:
    def __init__(self, raise_on_delete: ApiException | None = None) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.raise_on_delete = raise_on_delete

    def create_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, body: dict[str, object]
    ) -> None:
        self.create_calls.append(
            {"group": group, "version": version, "namespace": namespace, "plural": plural, "body": body}
        )

    def delete_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, name: str
    ) -> None:
        self.delete_calls.append(
            {"group": group, "version": version, "namespace": namespace, "plural": plural, "name": name}
        )
        if self.raise_on_delete is not None:
            raise self.raise_on_delete


class FakeNetworkingV1Api:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.raise_on_delete: Exception | None = None

    def create_namespaced_network_policy(self, namespace: str, body: object) -> None:
        self.create_calls.append((namespace, body))

    def delete_namespaced_network_policy(self, name: str, namespace: str) -> None:
        self.delete_calls.append((name, namespace))
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
