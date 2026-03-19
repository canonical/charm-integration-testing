# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from datetime import timedelta
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
class KubernetesBackendStub(KubernetesBackend):
    """Stub for KubernetesClient."""

    list_namespaced_pods_result: V1PodListStub | None = None
    list_namespaced_pods_raises: Exception | None = None
    get_namespaced_pod_result: V1Pod | None = None
    get_namespaced_pod_raises: Exception | None = None
    get_namespaced_pod_call_count: int = 0

    def __post_init__(self) -> None:
        self.CoreV1Api = self
        self.AppsV1Api = self

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
            mock_backend.CoreV1Api.read_namespaced_pod.side_effect = get_pod_side_effect

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
            mock_backend.CoreV1Api.read_namespaced_pod.side_effect = get_pod_side_effect

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
