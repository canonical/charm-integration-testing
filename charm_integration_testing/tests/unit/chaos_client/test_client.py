# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from unittest.mock import MagicMock

from chaos_client import NativeChaosClient
from juju import JujuBackend


class TestNativeChaosClientInit:
    """Test suite for NativeChaosClient initialization."""

    def test_init_with_backend(self) -> None:
        # GIVEN a juju backend
        mock_backend = MagicMock(spec=JujuBackend)

        # WHEN initializing the client with the backend
        client = NativeChaosClient(juju_backend=mock_backend)

        # THEN the backend is stored
        assert client._juju is mock_backend


class TestFillDisk:
    """Test suite for fill_disk method."""

    def test_dispatches_fallocate_command(self) -> None:
        # GIVEN a client wrapping a mock juju backend
        mock_backend = MagicMock(spec=JujuBackend)
        client = NativeChaosClient(juju_backend=mock_backend)

        # WHEN filling disk on a unit
        client.fill_disk(model="test-model", unit="postgresql/0", path="/tmp/fill", size_mb=512)

        # THEN exec_unit is called with the assembled fallocate command
        mock_backend.exec_unit.assert_called_once_with("test-model", "postgresql/0", "fallocate -l 512M /tmp/fill")


class TestStressCpu:
    """Test suite for stress_cpu method."""

    def test_dispatches_stress_ng_cpu_command(self) -> None:
        # GIVEN a client wrapping a mock juju backend
        mock_backend = MagicMock(spec=JujuBackend)
        client = NativeChaosClient(juju_backend=mock_backend)

        # WHEN stressing CPU on a unit
        client.stress_cpu(model="test-model", unit="postgresql/0", workers=4, duration=timedelta(seconds=30))

        # THEN exec_unit is called with the assembled stress-ng command
        mock_backend.exec_unit.assert_called_once_with("test-model", "postgresql/0", "stress-ng --cpu 4 --timeout 30s")

    def test_duration_is_rounded_down_to_whole_seconds(self) -> None:
        # GIVEN a client wrapping a mock juju backend
        mock_backend = MagicMock(spec=JujuBackend)
        client = NativeChaosClient(juju_backend=mock_backend)

        # WHEN stressing CPU with a sub-second duration component
        client.stress_cpu(model="test-model", unit="postgresql/0", workers=2, duration=timedelta(seconds=10.9))

        # THEN the duration is truncated to whole seconds
        mock_backend.exec_unit.assert_called_once_with("test-model", "postgresql/0", "stress-ng --cpu 2 --timeout 10s")


class TestStressMemory:
    """Test suite for stress_memory method."""

    def test_dispatches_stress_ng_vm_command(self) -> None:
        # GIVEN a client wrapping a mock juju backend
        mock_backend = MagicMock(spec=JujuBackend)
        client = NativeChaosClient(juju_backend=mock_backend)

        # WHEN stressing memory on a unit
        client.stress_memory(
            model="test-model", unit="postgresql/0", workers=2, size_mb=256, duration=timedelta(minutes=1)
        )

        # THEN exec_unit is called with the assembled stress-ng command
        mock_backend.exec_unit.assert_called_once_with(
            "test-model", "postgresql/0", "stress-ng --vm 2 --vm-bytes 256M --timeout 60s"
        )


class TestCleanup:
    """Test suite for cleanup method."""

    def test_removes_fill_path_and_kills_stress_ng(self) -> None:
        # GIVEN a client wrapping a mock juju backend
        mock_backend = MagicMock(spec=JujuBackend)
        client = NativeChaosClient(juju_backend=mock_backend)

        # WHEN cleaning up a unit
        client.cleanup(model="test-model", unit="postgresql/0", path="/tmp/fill")

        # THEN exec_unit is called to remove the fill file and kill stress-ng processes
        assert mock_backend.exec_unit.call_args_list == [
            (("test-model", "postgresql/0", "rm -f /tmp/fill"),),
            (("test-model", "postgresql/0", "pkill -f stress-ng || true"),),
        ]
