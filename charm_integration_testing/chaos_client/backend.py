# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from juju import JujuModelHandle


class StressEndedEarlyError(RuntimeError):
    """Stress ended before the requested observation window was verified."""


@dataclass(frozen=True)
class IoLatencyInjection:
    """Result of injecting I/O latency.

    ``injected_at`` is a `time.monotonic`-comparable timestamp taken when the experiment
    resource was actually created, i.e. when its duration began counting down. Callers
    must measure remaining observation time from this point, not from when io_latency()
    returns, since resolving resource constraints and awaiting controller-confirmed
    injection can both take a non-trivial amount of time.
    """

    duration: timedelta
    injected_at: float


class ChaosClient(ABC):
    """Chaos experiment interface.

    Use NotImplementedError only for unsupported operations, before any side effects.
    """

    def check_stress(self, model: JujuModelHandle, unit: str, *, allow_completed: bool = False) -> None:
        """Report known errors; allow completion only after independent fault confirmation.

        This check is not proof of sustained stress. Allowing completion
        must not suppress experiment errors or cleanup verification.
        """

    def check_io_latency(
        self, model: JujuModelHandle, unit: str, volume_path: str, *, allow_completed: bool = False
    ) -> None:
        """Report known errors; allow completion only once the latency duration has elapsed.

        This optional check is not proof of sustained latency injection. Allowing completion
        must not suppress experiment errors or cleanup verification.
        """
        raise NotImplementedError("Stress status checks are not supported by this client.")

    @abstractmethod
    def supports(self, operation: str) -> bool:
        """Report whether this client can currently run the named experiment operation.

        Must be side-effect free and consistent with whether the matching method would
        raise NotImplementedError, so callers can check support before resolving data
        (e.g. resource constraints) needed only to actually run the experiment.
        """
        raise NotImplementedError

    @abstractmethod
    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        raise NotImplementedError

    @abstractmethod
    def stress_memory(
        self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta
    ) -> timedelta | None:
        raise NotImplementedError

    @abstractmethod
    def io_latency(
        self,
        model: JujuModelHandle,
        unit: str,
        volume_path: str,
        delay: timedelta,
        percent: int,
        duration: timedelta,
    ) -> IoLatencyInjection:
        """Inject I/O latency and report the duration and start time actually used."""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def isolate_network(self, model: str, unit: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_network_isolation(self, model: str, unit: str) -> None:
        raise NotImplementedError
