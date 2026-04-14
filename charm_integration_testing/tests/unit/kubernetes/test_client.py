# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import logging
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiException, V1ObjectMeta, V1Pod, V1PodStatus  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend, KubernetesClient, PodStatus


def create_sample_pod(
    name: str = "test-pod",
    namespace: str = "default",
    phase: str = "Running",
    uid: str = "test-uid",
) -> V1Pod:
    """Helper to create a sample pod for testing."""
    return V1Pod(
        metadata=V1ObjectMeta(name=name, namespace=namespace, uid=uid),
        status=V1PodStatus(phase=phase),
    )


@dataclass
class V1PodListStub:
    items: list[V1Pod]
    metadata: None = None
    api_version: str = "v1"
    kind: str = "V1PodList"


@dataclass
class V1SecretStub:
    data: dict[str, bytes] | None


@dataclass
class KubernetesBackendStub(KubernetesBackend):
    """Stub for KubernetesClient."""

    list_namespaced_pods_result: V1PodListStub | None = None
    list_namespaced_pods_raises: Exception | None = None
    get_namespaced_pod_result: V1Pod | None = None
    get_namespaced_pod_raises: Exception | None = None
    get_namespaced_pod_call_count: int = 0
    patch_stateful_set_raises: Exception | None = None
    patch_stateful_set_call_count: int = 0
    patch_stateful_set_last_body: dict[str, Any] | None = None
    read_stateful_set_result: "V1StatefulSetStub | None" = None
    read_stateful_set_raises: Exception | None = None
    read_namespaced_secret_result: V1SecretStub | None = None
    read_namespaced_secret_raises: Exception | None = None

    def __post_init__(self) -> None:
        self.core_v1_api = self
        self.apps_v1_api = self

    def list_namespaced_pod(self, namespace: str) -> V1PodListStub:
        if self.list_namespaced_pods_raises:
            raise self.list_namespaced_pods_raises
        assert self.list_namespaced_pods_result is not None
        return self.list_namespaced_pods_result

    def read_namespaced_pod(self, pod_name: str, namespace: str) -> V1Pod:
        self.get_namespaced_pod_call_count += 1
        if self.get_namespaced_pod_raises:
            raise self.get_namespaced_pod_raises
        assert self.get_namespaced_pod_result is not None
        return self.get_namespaced_pod_result

    def patch_namespaced_stateful_set(self, name: str, namespace: str, body: dict[str, Any]) -> None:
        self.patch_stateful_set_call_count += 1
        self.patch_stateful_set_last_body = body
        if self.patch_stateful_set_raises:
            raise self.patch_stateful_set_raises

    def read_namespaced_stateful_set(self, name: str, namespace: str) -> "V1StatefulSetStub":
        if self.read_stateful_set_raises:
            raise self.read_stateful_set_raises
        assert self.read_stateful_set_result is not None
        return self.read_stateful_set_result

    def list_namespaced_stateful_set(self, namespace: str, **kwargs: Any) -> None:
        pass

    def read_namespaced_secret(self, name: str, namespace: str) -> V1SecretStub:
        if self.read_namespaced_secret_raises:
            raise self.read_namespaced_secret_raises
        assert self.read_namespaced_secret_result is not None
        return self.read_namespaced_secret_result


@dataclass
class V1StatefulSetStatusStub:
    observed_generation: int
    updated_replicas: int | None
    ready_replicas: int | None


@dataclass
class V1StatefulSetSpecStub:
    replicas: int | None


@dataclass
class V1StatefulSetStub:
    metadata: V1ObjectMeta
    status: V1StatefulSetStatusStub
    spec: V1StatefulSetSpecStub


@dataclass
class WatchStub:
    events: list[dict[str, Any]]
    stop_call_count: int = 0

    def stream(self, func: Callable[..., Any], **kwargs: Any) -> Iterator[dict[str, Any]]:
        return iter(self.events)

    def stop(self) -> None:
        self.stop_call_count += 1


def create_statefulset_stub(
    generation: int,
    observed_generation: int,
    updated_replicas: int | None,
    ready_replicas: int | None,
    replicas: int | None,
) -> V1StatefulSetStub:
    """Helper to create a V1StatefulSetStub for testing."""
    return V1StatefulSetStub(
        metadata=V1ObjectMeta(generation=generation),
        status=V1StatefulSetStatusStub(
            observed_generation=observed_generation,
            updated_replicas=updated_replicas,
            ready_replicas=ready_replicas,
        ),
        spec=V1StatefulSetSpecStub(replicas=replicas),
    )


class TestPodStatus:
    """Test suite for PodStatus enum."""

    def test_enum_values(self) -> None:
        # GIVEN PodStatus enum
        # WHEN checking values
        # THEN all expected statuses exist
        assert PodStatus.PENDING.value == "Pending"
        assert PodStatus.RUNNING.value == "Running"
        assert PodStatus.SUCCEEDED.value == "Succeeded"
        assert PodStatus.FAILED.value == "Failed"
        assert PodStatus.UNKNOWN.value == "Unknown"


class TestKubernetesClientInit:
    """Test suite for KubernetesClient initialization."""

    def test_init_with_client(self) -> None:
        # GIVEN a custom client
        backend = KubernetesBackendStub()

        # WHEN initializing client with backend
        client = KubernetesClient(backend=backend)
        # THEN the custom backend is used
        assert client.backend is backend

    def test_init_without_logger(self) -> None:
        # GIVEN no custom logger
        # WHEN initializing backend without logger
        backend = KubernetesBackendStub()
        client = KubernetesClient(backend=backend)

        # THEN a default logger is created
        assert client.logger is not None
        assert isinstance(client.logger, logging.Logger)

    def test_init_with_custom_timeouts(self) -> None:
        # GIVEN custom timeout and delay values
        custom_timeout = timedelta(minutes=10)
        custom_delay = timedelta(seconds=2)
        backend = KubernetesBackendStub()
        # WHEN initializing backend with custom values
        client = KubernetesClient(backend=backend, default_timeout=custom_timeout, default_delay=custom_delay)

        # THEN the custom values are set
        assert client.default_timeout == custom_timeout
        assert client.default_delay == custom_delay

    def test_init_with_default_timeouts(self) -> None:
        # GIVEN no custom timeout and delay values
        # WHEN initializing backend with defaults
        backend = KubernetesBackendStub()
        client = KubernetesClient(backend=backend)

        # THEN default values are set
        assert client.default_timeout == timedelta(minutes=5)
        assert client.default_delay == timedelta(seconds=1)

    class TestGetCharmPods:
        """Test suite for get_charm_pods method."""

        def test_single_charm_pod(self) -> None:
            # GIVEN a backend with a pod matching charm name
            pod = create_sample_pod("postgresql-0", "test-model")
            pod_list = V1PodListStub(items=[pod])
            backend_stub = KubernetesBackendStub(list_namespaced_pods_result=pod_list)

            backend = KubernetesClient(backend=backend_stub)

            # WHEN getting charm pods
            result = backend.get_charm_pods("postgresql", "test-model")

            # THEN returns matching pods
            assert len(result) == 1
            assert result[0] == pod

        def test_multiple_charm_pods(self) -> None:
            # GIVEN a backend with multiple pods matching charm name
            pod1 = create_sample_pod("postgresql-0", "test-model")
            pod2 = create_sample_pod("postgresql-1", "test-model")
            pod3 = create_sample_pod("redis-0", "test-model")
            pod_list = V1PodListStub(items=[pod1, pod2, pod3])
            backend_stub = KubernetesBackendStub(list_namespaced_pods_result=pod_list)

            backend = KubernetesClient(backend=backend_stub)

            # WHEN getting charm pods
            result = backend.get_charm_pods("postgresql", "test-model")

            # THEN returns only matching pods
            assert len(result) == 2
            assert pod1 in result
            assert pod2 in result
            assert pod3 not in result

        def test_no_matching_pods(self) -> None:
            # GIVEN a backend with no matching pods
            pod = create_sample_pod("redis-0", "test-model")
            pod_list = V1PodListStub(items=[pod])
            backend_stub = KubernetesBackendStub(list_namespaced_pods_result=pod_list)

            backend = KubernetesClient(backend=backend_stub)

            # WHEN getting charm pods
            result = backend.get_charm_pods("postgresql", "test-model")

            # THEN returns empty list
            assert len(result) == 0

        def test_empty_namespace(self) -> None:
            # GIVEN a backend with no pods
            pod_list = V1PodListStub(items=[])
            backend_stub = KubernetesBackendStub(list_namespaced_pods_result=pod_list)

            backend = KubernetesClient(backend=backend_stub)

            # WHEN getting charm pods
            result = backend.get_charm_pods("postgresql", "test-model")

            # THEN returns empty list
            assert len(result) == 0

        def test_api_exception(self) -> None:
            # GIVEN a backend that raises ApiException
            api_error = ApiException(status=500, reason="Internal Server Error")
            backend_stub = KubernetesBackendStub(list_namespaced_pods_raises=api_error)

            backend = KubernetesClient(backend=backend_stub)

            # WHEN getting charm pods
            # THEN raises ApiException
            with pytest.raises(ApiException) as exc_info:
                backend.get_charm_pods("postgresql", "test-model")

            assert exc_info.value.status == 500

    class TestWait:
        """Test suite for wait method."""

        def test_check_returns_immediately(self) -> None:
            # GIVEN a check function that succeeds on first call
            client = KubernetesClient(
                backend=KubernetesBackendStub(),
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting with a check that returns immediately
            result = client.wait(
                check=lambda: True,
                timeout_message="should not timeout",
            )

            # THEN returns the check result
            assert result is True

        @patch("kubernetes_client.client.sleep")
        def test_check_succeeds_after_retries(self, mock_sleep: MagicMock) -> None:
            # GIVEN a check function that fails twice then succeeds
            call_count = 0

            def check() -> str | None:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    return None
                return "done"

            client = KubernetesClient(
                backend=KubernetesBackendStub(),
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting
            result = client.wait(
                check=check,
                timeout_message="should not timeout",
            )

            # THEN sleep is called between checks and result is returned
            assert result == "done"
            assert mock_sleep.call_count >= 2

        @patch("kubernetes_client.client.sleep")
        @patch("kubernetes_client.client.datetime")
        def test_timeout(self, mock_datetime: MagicMock, mock_sleep: MagicMock) -> None:
            # GIVEN a check function that never succeeds and time passes
            from datetime import datetime

            start_time = datetime(2026, 3, 16, 10, 0, 0)
            timeout_time = datetime(2026, 3, 16, 10, 5, 1)  # Just over 5 minutes

            mock_datetime.now.side_effect = [start_time, timeout_time]

            backend = KubernetesClient(
                backend=KubernetesBackendStub(),
                default_timeout=timedelta(minutes=5),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting with a check that never succeeds
            # THEN raises TimeoutError with the provided message
            with pytest.raises(TimeoutError) as exc_info:
                backend.wait(
                    check=lambda: None,
                    timeout_message="timed out waiting",
                )

            assert "timed out waiting" in str(exc_info.value)

        @patch("kubernetes_client.client.sleep")
        def test_custom_timeout_and_delay(self, mock_sleep: MagicMock) -> None:
            # GIVEN custom timeout and delay
            backend = KubernetesClient(
                backend=KubernetesBackendStub(),
            )

            custom_timeout = timedelta(minutes=10)
            custom_delay = timedelta(seconds=2)

            # WHEN waiting with custom values and an immediately succeeding check
            result = backend.wait(
                check=lambda: True,
                timeout_message="should not timeout",
                timeout=custom_timeout,
                delay=custom_delay,
            )

            # THEN completes successfully
            assert result is True

        def test_check_exception_propagates(self) -> None:
            # GIVEN a check function that raises an exception
            client = KubernetesClient(
                backend=KubernetesBackendStub(),
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            def check() -> bool | None:
                raise ApiException(status=500, reason="Internal Server Error")

            # WHEN waiting
            # THEN the exception propagates
            with pytest.raises(ApiException) as exc_info:
                client.wait(
                    check=check,
                    timeout_message="should not timeout",
                )

            assert exc_info.value.status == 500

    class TestWaitForPodRecreation:
        """Test suite for wait_for_pod_recreation method."""

        def test_pod_already_recreated_and_running(self) -> None:
            # GIVEN a recreated pod that is already running
            new_pod = create_sample_pod("test-pod", "test-namespace", "Running", "new-uid")
            backend_stub = KubernetesBackendStub(get_namespaced_pod_result=new_pod)

            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for pod recreation
            result = client.wait_for_pod_recreation(
                pod_name="test-pod",
                namespace="test-namespace",
                old_uid="old-uid",
                target_status=PodStatus.RUNNING,
            )

            # THEN returns the new pod
            assert result == new_pod
            assert result.metadata.uid == "new-uid"

        @patch("kubernetes_client.client.sleep")
        def test_pod_recreated_after_deletion(self, mock_sleep: MagicMock) -> None:
            # GIVEN a pod that is deleted then recreated
            old_pod = create_sample_pod("test-pod", "test-namespace", "Running", "old-uid")
            new_pending_pod = create_sample_pod("test-pod", "test-namespace", "Pending", "new-uid")
            new_running_pod = create_sample_pod("test-pod", "test-namespace", "Running", "new-uid")

            call_count = 0

            def get_pod_side_effect(pod_name: str, namespace: str) -> V1Pod | None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return old_pod  # Still old pod
                elif call_count == 2:
                    raise ApiException(status=404, reason="Not Found")  # Pod deleted
                elif call_count == 3:
                    return new_pending_pod  # New pod created but pending
                else:
                    return new_running_pod  # New pod running

            mock_backend = MagicMock()
            mock_backend.core_v1_api.read_namespaced_pod.side_effect = get_pod_side_effect

            client = KubernetesClient(
                backend=mock_backend,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for pod recreation
            result = client.wait_for_pod_recreation(
                pod_name="test-pod",
                namespace="test-namespace",
                old_uid="old-uid",
                target_status=PodStatus.RUNNING,
            )

            # THEN returns the new running pod
            assert result.metadata.uid == "new-uid"
            assert result.status.phase == "Running"
            assert mock_sleep.call_count >= 3

        @patch("kubernetes_client.client.sleep")
        @patch("kubernetes_client.client.datetime")
        def test_timeout_waiting_for_recreation(self, mock_datetime: MagicMock, mock_sleep: MagicMock) -> None:
            # GIVEN a pod that never gets recreated and time passes
            from datetime import datetime

            start_time = datetime(2026, 3, 16, 10, 0, 0)
            timeout_time = datetime(2026, 3, 16, 10, 5, 1)  # Just over 5 minutes

            mock_datetime.now.side_effect = [start_time, timeout_time]

            old_pod = create_sample_pod("test-pod", "test-namespace", "Running", "old-uid")
            backend_stub = KubernetesBackendStub(get_namespaced_pod_result=old_pod)

            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(minutes=5),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for pod that never recreates
            # THEN raises TimeoutError
            with pytest.raises(TimeoutError) as exc_info:
                client.wait_for_pod_recreation(
                    pod_name="test-pod",
                    namespace="test-namespace",
                    old_uid="old-uid",
                    target_status=PodStatus.RUNNING,
                )

            assert "was not recreated or did not reach Running status within timeout" in str(exc_info.value)

        @patch("kubernetes_client.client.sleep")
        def test_custom_target_status(self, mock_sleep: MagicMock) -> None:
            # GIVEN a pod recreated with custom target status
            new_pod = create_sample_pod("test-pod", "test-namespace", "Succeeded", "new-uid")
            backend_stub = KubernetesBackendStub(get_namespaced_pod_result=new_pod)

            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for pod recreation with custom status
            result = client.wait_for_pod_recreation(
                pod_name="test-pod",
                namespace="test-namespace",
                old_uid="old-uid",
                target_status=PodStatus.SUCCEEDED,
            )

            # THEN returns pod with correct status
            assert result == new_pod
            assert result.status.phase == "Succeeded"

        @patch("kubernetes_client.client.sleep")
        def test_404_handled_gracefully(self, mock_sleep: MagicMock) -> None:
            # GIVEN a pod that returns 404 then appears
            new_pod = create_sample_pod("test-pod", "test-namespace", "Running", "new-uid")

            call_count = 0

            def get_pod_side_effect(pod_name: str, namespace: str) -> V1Pod:
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise ApiException(status=404, reason="Not Found")
                return new_pod

            mock_backend = MagicMock()
            mock_backend.core_v1_api.read_namespaced_pod.side_effect = get_pod_side_effect

            client = KubernetesClient(
                backend=mock_backend,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for pod recreation
            result = client.wait_for_pod_recreation(
                pod_name="test-pod",
                namespace="test-namespace",
                old_uid="old-uid",
                target_status=PodStatus.RUNNING,
            )

            # THEN eventually returns the pod
            assert result == new_pod
            assert mock_sleep.call_count >= 2

        def test_non_404_api_exception(self) -> None:
            # GIVEN a client that raises non-404 ApiException
            api_error = ApiException(status=500, reason="Internal Server Error")
            backend_stub = KubernetesBackendStub(get_namespaced_pod_raises=api_error)

            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for pod recreation
            # THEN raises ApiException
            with pytest.raises(ApiException) as exc_info:
                client.wait_for_pod_recreation(
                    pod_name="test-pod",
                    namespace="test-namespace",
                    old_uid="old-uid",
                    target_status=PodStatus.RUNNING,
                )

            assert exc_info.value.status == 500

    class TestRestartStatefulset:
        """Test suite for restart_statefulset method."""

        def test_success(self) -> None:
            # GIVEN a backend stub
            backend_stub = KubernetesBackendStub()
            client = KubernetesClient(backend=backend_stub)

            # WHEN restarting a statefulset
            client.restart_statefulset(namespace="test-ns", statefulset_name="my-sts")

            # THEN patch_namespaced_stateful_set is called once with the correct name, namespace,
            # and a body containing the restartedAt annotation
            assert backend_stub.patch_stateful_set_call_count == 1
            assert backend_stub.patch_stateful_set_last_body is not None
            annotations = backend_stub.patch_stateful_set_last_body["spec"]["template"]["metadata"]["annotations"]
            assert "kubectl.kubernetes.io/restartedAt" in annotations

        def test_api_exception_propagates(self) -> None:
            # GIVEN a backend stub configured to raise ApiException on patch
            api_error = ApiException(status=500, reason="Internal Server Error")
            backend_stub = KubernetesBackendStub(patch_stateful_set_raises=api_error)
            client = KubernetesClient(backend=backend_stub)

            # WHEN restarting a statefulset
            # THEN the ApiException is re-raised
            with pytest.raises(ApiException) as exc_info:
                client.restart_statefulset(namespace="test-ns", statefulset_name="my-sts")

            assert exc_info.value.status == 500

    class TestWaitForStatefulsetRestart:
        """Test suite for wait_for_statefulset_restart method."""

        @patch("kubernetes_client.client.watch.Watch")
        def test_success_returns_when_all_replicas_ready(self, MockWatch: MagicMock) -> None:
            # GIVEN a watch stream delivering a single fully-rolled-out event
            ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=3, ready_replicas=3, replicas=3
            )
            watch_stub = WatchStub(events=[{"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=2, updated_replicas=0, ready_replicas=0, replicas=3
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts", timeout_seconds=60)

            # THEN no exception is raised and the watcher is stopped via the finally block
            assert watch_stub.stop_call_count == 1

        @patch("kubernetes_client.client.watch.Watch")
        def test_skips_events_below_target_generation(self, MockWatch: MagicMock) -> None:
            # GIVEN a stream where the first event has a stale observed_generation
            # and the second event is at the current generation with all replicas ready
            stale_sts = create_statefulset_stub(
                generation=2, observed_generation=1, updated_replicas=2, ready_replicas=2, replicas=2
            )
            ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=2, ready_replicas=2, replicas=2
            )
            watch_stub = WatchStub(events=[{"object": stale_sts}, {"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=1, updated_replicas=0, ready_replicas=0, replicas=2
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            # THEN the method returns without raising (stale event was skipped)
            assert watch_stub.stop_call_count == 1

        @patch("kubernetes_client.client.watch.Watch")
        def test_waits_while_pods_still_updating(self, MockWatch: MagicMock) -> None:
            # GIVEN a stream where the first event shows fewer updated pods than desired
            updating_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=1, ready_replicas=3, replicas=3
            )
            ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=3, ready_replicas=3, replicas=3
            )
            watch_stub = WatchStub(events=[{"object": updating_sts}, {"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=2, updated_replicas=0, ready_replicas=0, replicas=3
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            # THEN the method returns without raising once all pods have been updated

        @patch("kubernetes_client.client.watch.Watch")
        def test_waits_while_pods_not_yet_ready(self, MockWatch: MagicMock) -> None:
            # GIVEN a stream where pods are updated but not yet ready (failing readiness probes)
            not_ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=3, ready_replicas=1, replicas=3
            )
            ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=3, ready_replicas=3, replicas=3
            )
            watch_stub = WatchStub(events=[{"object": not_ready_sts}, {"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=2, updated_replicas=0, ready_replicas=0, replicas=3
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            # THEN the method returns without raising once all pods pass their readiness probes

        @patch("kubernetes_client.client.watch.Watch")
        def test_raises_timeout_when_stream_exhausted(self, MockWatch: MagicMock) -> None:
            # GIVEN a watch stream that ends before the rollout completes (timeout_seconds exceeded)
            watch_stub = WatchStub(events=[])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=2, updated_replicas=0, ready_replicas=0, replicas=2
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            # THEN a TimeoutError is raised with the statefulset name in the message
            with pytest.raises(TimeoutError) as exc_info:
                client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            assert "my-sts" in str(exc_info.value)
            # AND the watcher is still stopped via the finally block
            assert watch_stub.stop_call_count == 1

        def test_initial_read_api_exception_propagates(self) -> None:
            # GIVEN a backend where the initial StatefulSet read raises ApiException
            api_error = ApiException(status=404, reason="Not Found")
            backend_stub = KubernetesBackendStub(read_stateful_set_raises=api_error)
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            # THEN the ApiException is re-raised before the watch loop begins
            with pytest.raises(ApiException) as exc_info:
                client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            assert exc_info.value.status == 404

        @patch("kubernetes_client.client.watch.Watch")
        def test_none_spec_replicas_defaults_to_one(self, MockWatch: MagicMock) -> None:
            # GIVEN a StatefulSet whose spec.replicas is None (treated as 1 by the client)
            ready_sts = create_statefulset_stub(
                generation=1, observed_generation=1, updated_replicas=1, ready_replicas=1, replicas=None
            )
            watch_stub = WatchStub(events=[{"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=1, observed_generation=1, updated_replicas=0, ready_replicas=0, replicas=None
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            # THEN replicas=None is treated as 1 and the method returns without raising

        @patch("kubernetes_client.client.watch.Watch")
        def test_none_updated_and_ready_replicas_treated_as_zero(self, MockWatch: MagicMock) -> None:
            # GIVEN an event where updated_replicas and ready_replicas are None (rollout just started),
            # followed by an event where all pods are updated and ready
            starting_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=None, ready_replicas=None, replicas=2
            )
            ready_sts = create_statefulset_stub(
                generation=2, observed_generation=2, updated_replicas=2, ready_replicas=2, replicas=2
            )
            watch_stub = WatchStub(events=[{"object": starting_sts}, {"object": ready_sts}])
            MockWatch.return_value = watch_stub
            backend_stub = KubernetesBackendStub(
                read_stateful_set_result=create_statefulset_stub(
                    generation=2, observed_generation=2, updated_replicas=0, ready_replicas=0, replicas=2
                )
            )
            client = KubernetesClient(backend=backend_stub)

            # WHEN waiting for the statefulset restart
            client.wait_for_statefulset_restart(namespace="test-ns", statefulset_name="my-sts")

            # THEN None counts are treated as 0 so the first event is skipped, and the method
            # returns without raising once actual counts reach the desired replica count

    class TestWaitForApplicationConfigSecret:
        """Test suite for wait_for_application_config_secret method."""

        def _make_secret(self, **kwargs: str) -> V1SecretStub:
            """Return a V1SecretStub whose data values are base64-encoded."""
            return V1SecretStub(data={key: base64.b64encode(value.encode()) for key, value in kwargs.items()})

        @patch("kubernetes_client.client.socket.getaddrinfo")
        def test_cluster_local_host_resolves_immediately(self, mock_getaddrinfo: MagicMock) -> None:
            # GIVEN a secret whose only cluster-local host resolves on the first check
            secret = self._make_secret(controller="controller.test-ns.svc.cluster.local:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret to converge
            client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            # THEN socket.getaddrinfo is called with the host (port stripped)
            mock_getaddrinfo.assert_called_once_with("controller.test-ns.svc.cluster.local", None)

        def test_non_cluster_local_hosts_skipped(self) -> None:
            # GIVEN a secret with only external (non-.svc.cluster.local) addresses
            secret = self._make_secret(addresses="10.0.0.1:17070,192.168.1.2:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret to converge
            # THEN no DNS lookups are performed and the method returns immediately
            with patch("kubernetes_client.client.socket.getaddrinfo") as mock_getaddrinfo:
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")
                mock_getaddrinfo.assert_not_called()

        def test_empty_data_dict_waits(self) -> None:
            # GIVEN a secret whose data dict is empty (treated as falsy, same as no data)
            secret = V1SecretStub(data={})
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(milliseconds=200),
                default_delay=timedelta(milliseconds=50),
            )

            # WHEN waiting for the secret to converge
            # THEN the method times out because an empty dict is falsy and treated as no data
            with pytest.raises(TimeoutError):
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

        @patch("kubernetes_client.client.sleep")
        def test_secret_not_found_then_found(self, mock_sleep: MagicMock) -> None:
            # GIVEN the secret is absent (404) on the first check, then present on the second
            secret = self._make_secret(controller="controller.test-ns.svc.cluster.local:17070")
            call_count = 0

            def read_secret(name: str, namespace: str) -> V1SecretStub:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ApiException(status=404, reason="Not Found")
                return secret

            mock_backend = MagicMock()
            mock_backend.core_v1_api.read_namespaced_secret.side_effect = read_secret
            client = KubernetesClient(
                backend=mock_backend,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for the secret to converge
            with patch("kubernetes_client.client.socket.getaddrinfo"):
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            # THEN the method retried after the 404
            assert mock_sleep.call_count >= 1

        @patch("kubernetes_client.client.sleep")
        def test_secret_has_no_data_then_populated(self, mock_sleep: MagicMock) -> None:
            # GIVEN a secret that initially has no data, then is populated
            secret_no_data = V1SecretStub(data=None)
            secret_with_data = self._make_secret(controller="controller.test-ns.svc.cluster.local:17070")
            results = iter([secret_no_data, secret_with_data])
            mock_backend = MagicMock()
            mock_backend.core_v1_api.read_namespaced_secret.side_effect = lambda name, namespace: next(results)
            client = KubernetesClient(
                backend=mock_backend,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for the secret to converge
            with patch("kubernetes_client.client.socket.getaddrinfo"):
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            # THEN the method retried after seeing no data
            assert mock_sleep.call_count >= 1

        @patch("kubernetes_client.client.sleep")
        def test_unresolvable_host_then_resolves(self, mock_sleep: MagicMock) -> None:
            # GIVEN a cluster-local host that fails DNS twice then succeeds
            secret = self._make_secret(controller="controller.test-ns.svc.cluster.local:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            gaierror_call_count = 0

            def getaddrinfo_side_effect(host: str, port: object) -> list[object]:
                nonlocal gaierror_call_count
                gaierror_call_count += 1
                if gaierror_call_count <= 2:
                    raise socket.gaierror("Name or service not known")
                return [("AF_INET", None, None, None, ("10.0.0.1", 0))]

            # WHEN waiting for the secret to converge
            with patch("kubernetes_client.client.socket.getaddrinfo", side_effect=getaddrinfo_side_effect):
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            # THEN the method retried until DNS resolved
            assert mock_sleep.call_count >= 2

        @patch("kubernetes_client.client.sleep")
        @patch("kubernetes_client.client.datetime")
        def test_timeout_when_host_never_resolves(self, mock_datetime: MagicMock, mock_sleep: MagicMock) -> None:
            # GIVEN the cluster-local host never resolves
            from datetime import datetime

            start_time = datetime(2026, 4, 14, 10, 0, 0)
            timeout_time = datetime(2026, 4, 14, 10, 5, 1)
            mock_datetime.now.side_effect = [start_time, timeout_time]

            secret = self._make_secret(controller="controller.test-ns.svc.cluster.local:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(minutes=5),
                default_delay=timedelta(seconds=1),
            )

            # WHEN waiting for the secret to converge
            # THEN a TimeoutError is raised containing the secret name
            with patch(
                "kubernetes_client.client.socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")
            ):
                with pytest.raises(TimeoutError) as exc_info:
                    client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            assert "myapp-application-config" in str(exc_info.value)

        def test_non_404_api_exception_propagates(self) -> None:
            # GIVEN the Kubernetes API returns a 500 error
            api_error = ApiException(status=500, reason="Internal Server Error")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_raises=api_error)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret to converge
            # THEN the ApiException is re-raised
            with pytest.raises(ApiException) as exc_info:
                client.wait_for_application_config_secret(namespace="test-ns", application="myapp")

            assert exc_info.value.status == 500

        @patch("kubernetes_client.client.socket.getaddrinfo")
        def test_comma_separated_entries_all_checked(self, mock_getaddrinfo: MagicMock) -> None:
            # GIVEN a single key whose value contains two cluster-local entries
            secret = self._make_secret(addresses="ctrl-a.ns.svc.cluster.local:17070,ctrl-b.ns.svc.cluster.local:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret to converge
            client.wait_for_application_config_secret(namespace="ns", application="myapp")

            # THEN both hosts are looked up
            assert mock_getaddrinfo.call_count == 2
            mock_getaddrinfo.assert_any_call("ctrl-a.ns.svc.cluster.local", None)
            mock_getaddrinfo.assert_any_call("ctrl-b.ns.svc.cluster.local", None)

        @patch("kubernetes_client.client.socket.getaddrinfo")
        def test_port_stripped_before_getaddrinfo(self, mock_getaddrinfo: MagicMock) -> None:
            # GIVEN a value with a host:port entry
            secret = self._make_secret(controller="controller.model.svc.cluster.local:17070")
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret to converge
            client.wait_for_application_config_secret(namespace="model", application="myapp")

            # THEN socket.getaddrinfo is called with the host only, port stripped
            mock_getaddrinfo.assert_called_once_with("controller.model.svc.cluster.local", None)

        @patch("kubernetes_client.client.socket.getaddrinfo")
        def test_multiple_keys_all_must_resolve(self, mock_getaddrinfo: MagicMock) -> None:
            # GIVEN a secret with two keys, each containing a cluster-local host
            secret = self._make_secret(
                key1="ctrl-a.ns.svc.cluster.local:17070",
                key2="ctrl-b.ns.svc.cluster.local:17070",
            )
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN both hosts resolve
            client.wait_for_application_config_secret(namespace="ns", application="myapp")

            # THEN both hosts are looked up and the method returns without error
            assert mock_getaddrinfo.call_count == 2

        @patch("kubernetes_client.client.sleep")
        def test_one_of_multiple_keys_unresolvable_keeps_waiting(self, mock_sleep: MagicMock) -> None:
            # GIVEN a secret with two keys; the second key's host initially fails DNS
            secret = self._make_secret(
                key1="ctrl-a.ns.svc.cluster.local:17070",
                key2="ctrl-b.ns.svc.cluster.local:17070",
            )
            backend_stub = KubernetesBackendStub(read_namespaced_secret_result=secret)
            client = KubernetesClient(
                backend=backend_stub,
                default_timeout=timedelta(seconds=10),
                default_delay=timedelta(seconds=1),
            )

            fail_count = 0

            def getaddrinfo_side_effect(host: str, port: object) -> list[object]:
                nonlocal fail_count
                if "ctrl-b" in host and fail_count < 2:
                    fail_count += 1
                    raise socket.gaierror("Name or service not known")
                return [("AF_INET", None, None, None, ("10.0.0.1", 0))]

            # WHEN waiting for the secret to converge
            with patch("kubernetes_client.client.socket.getaddrinfo", side_effect=getaddrinfo_side_effect):
                client.wait_for_application_config_secret(namespace="ns", application="myapp")

            # THEN the method retried until both hosts resolved
            assert mock_sleep.call_count >= 2

        @patch("kubernetes_client.client.socket.getaddrinfo")
        def test_secret_name_uses_application_name(self, mock_getaddrinfo: MagicMock) -> None:
            # GIVEN a backend that records the secret name requested
            requested_names: list[str] = []

            def read_secret(name: str, namespace: str) -> V1SecretStub:
                requested_names.append(name)
                # Return a secret with only external addresses so the check passes immediately
                return V1SecretStub(data={"addr": base64.b64encode(b"10.0.0.1:17070")})

            mock_backend = MagicMock()
            mock_backend.core_v1_api.read_namespaced_secret.side_effect = read_secret
            client = KubernetesClient(
                backend=mock_backend,
                default_timeout=timedelta(seconds=5),
                default_delay=timedelta(milliseconds=100),
            )

            # WHEN waiting for the secret
            client.wait_for_application_config_secret(namespace="model", application="postgresql")

            # THEN the secret name is {application}-application-config
            assert requested_names == ["postgresql-application-config"]
