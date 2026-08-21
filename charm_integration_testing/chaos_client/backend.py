# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from datetime import timedelta

from juju import JujuModelHandle


class ChaosClient(ABC):
    @abstractmethod
    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        raise NotImplementedError

    @abstractmethod
    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        raise NotImplementedError
