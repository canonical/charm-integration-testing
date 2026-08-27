# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from chaos_client import NativeChaosClient
from juju.backend import JujuExecOutput

from ..extensions.shared import NullJujuBackend


@dataclass
class JujuStub(NullJujuBackend):
    exec_calls: list[tuple[str, str, str, bool]] = field(default_factory=list)

    def exec_unit(self, model: str, unit: str, task: str, operator: bool = False) -> JujuExecOutput:
        self.exec_calls.append((model, unit, task, operator))
        return JujuExecOutput(return_code=0, stdout="", stderr="")


class TestNativeChaosClientInit:
    """Test suite for NativeChaosClient initialization."""

    def test_init_with_backend(self) -> None:
        # GIVEN a juju backend
        stub = JujuStub()

        # WHEN initializing the client with the backend
        client = NativeChaosClient(juju_backend=stub)

        # THEN the backend is stored
        assert client._juju is stub
        assert stub.exec_calls == []


class TestFillDisk:
    """Test suite for fill_disk method."""

    def test_dispatches_fallocate_command(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN filling disk on a unit
        client.fill_disk(model="test-model", unit="postgresql/0", path="/tmp/fill", size_mb=512)

        # THEN exec_unit is called with the assembled fallocate command
        assert stub.exec_calls == [("test-model", "postgresql/0", "fallocate -l 512M -- /tmp/fill", False)]


class TestStressCpu:
    """Test suite for stress_cpu method."""

    def test_dispatches_stress_ng_cpu_command(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN stressing CPU on a unit
        client.stress_cpu(model="test-model", unit="postgresql/0", workers=4, duration=timedelta(seconds=30))

        # THEN exec_unit is called with the assembled stress-ng command
        assert stub.exec_calls == [("test-model", "postgresql/0", "stress-ng --cpu 4 --timeout 30s", False)]

    def test_duration_is_rounded_down_to_whole_seconds(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN stressing CPU with a sub-second duration component
        client.stress_cpu(model="test-model", unit="postgresql/0", workers=2, duration=timedelta(seconds=10.9))

        # THEN the duration is truncated to whole seconds
        assert stub.exec_calls == [("test-model", "postgresql/0", "stress-ng --cpu 2 --timeout 10s", False)]


class TestStressMemory:
    """Test suite for stress_memory method."""

    def test_dispatches_stress_ng_vm_command(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN stressing memory on a unit
        client.stress_memory(
            model="test-model", unit="postgresql/0", workers=2, size_mb=256, duration=timedelta(minutes=1)
        )

        # THEN exec_unit is called with the assembled stress-ng command
        assert stub.exec_calls == [
            ("test-model", "postgresql/0", "stress-ng --vm 2 --vm-bytes 256M --timeout 60s", False)
        ]


class TestCleanup:
    """Test suite for cleanup method."""

    def test_removes_fill_path_and_kills_stress_ng(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN cleaning up a unit
        client.cleanup(model="test-model", unit="postgresql/0", path="/tmp/fill")

        # THEN exec_unit is called to remove the fill file and kill stress-ng processes
        assert stub.exec_calls == [
            ("test-model", "postgresql/0", "rm -f -- /tmp/fill", False),
            ("test-model", "postgresql/0", "pkill -f stress-ng || true", False),
        ]


class TestIsolateNetwork:
    """Test suite for isolate_network method."""

    def test_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN isolating network on a unit
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.isolate_network(model="test-model", unit="postgresql/0")


class TestRemoveNetworkIsolation:
    """Test suite for remove_network_isolation method."""

    def test_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a juju backend stub
        stub = JujuStub()
        client = NativeChaosClient(juju_backend=stub)

        # WHEN removing network isolation on a unit
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.remove_network_isolation(model="test-model", unit="postgresql/0")
