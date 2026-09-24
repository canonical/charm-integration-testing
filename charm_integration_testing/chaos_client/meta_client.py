# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, NoReturn

from juju import JujuModelHandle

from .backend import ChaosClient


class ChaosNotSupportedError(NotImplementedError):
    """No configured client supports the requested experiment."""


def _raise_unsupported(operation: str) -> NoReturn:
    raise ChaosNotSupportedError(f"No configured chaos client supports '{operation}'.")


class ChaosCleanupError(RuntimeError):
    """Raised when one or more cleanup operations fail."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"{len(errors)} chaos cleanup operation(s) failed.")


@dataclass(frozen=True)
class _CleanupAction:
    scope: tuple[str, str]
    path: str
    run: Callable[[], None]


class MetaChaosClient(ChaosClient):
    """Try clients in order for each experiment, falling back on NotImplementedError.

    Do not share this instance or its clients between tests.
    """

    def __init__(
        self, tools: list[ChaosClient], *, on_unsupported: Callable[[str], NoReturn] = _raise_unsupported
    ) -> None:
        self._tools = tuple(tools)
        self._on_unsupported = on_unsupported
        self._cleanups: list[_CleanupAction] = []
        self._network_cleanups: list[_CleanupAction] = []

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        self._dispatch(
            "fill_disk",
            lambda tool: tool.fill_disk(model, unit, path, size_mb),
            lambda tool: self._experiment_cleanup(tool, model, unit, path),
            self._cleanups,
        )

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        self._dispatch(
            "stress_cpu",
            lambda tool: tool.stress_cpu(model, unit, workers, duration),
            lambda tool: self._experiment_cleanup(tool, model, unit),
            self._cleanups,
        )

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        self._dispatch(
            "stress_memory",
            lambda tool: tool.stress_memory(model, unit, workers, size_mb, duration),
            lambda tool: self._experiment_cleanup(tool, model, unit),
            self._cleanups,
        )

    def io_latency(
        self,
        model: JujuModelHandle,
        unit: str,
        volume_path: str,
        delay: timedelta,
        percent: int,
        duration: timedelta,
    ) -> None:
        self._dispatch(
            "io_latency",
            lambda tool: tool.io_latency(model, unit, volume_path, delay, percent, duration),
            lambda tool: self._experiment_cleanup(tool, model, unit, volume_path),
            self._cleanups,
        )

    def isolate_network(self, model: str, unit: str) -> None:
        self._dispatch(
            "isolate_network",
            lambda tool: tool.isolate_network(model, unit),
            lambda tool: _CleanupAction((model, unit), "", lambda: tool.remove_network_isolation(model, unit)),
            self._network_cleanups,
        )

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        """Clean the requested path, or CPU and memory stress when path is empty."""
        self._cleanup(self._cleanups, (model.uri, unit), path)

    def remove_network_isolation(self, model: str, unit: str) -> None:
        self._cleanup(self._network_cleanups, (model, unit), "")

    def cleanup_all(self) -> None:
        """Clean up all pending experiments, including network isolation."""
        errors: list[Exception] = []
        for pending in (self._network_cleanups, self._cleanups):
            try:
                self._cleanup(pending, None, "")
            except ChaosCleanupError as error:
                errors.extend(error.errors)
        if errors:
            raise ChaosCleanupError(errors) from errors[0]

    @staticmethod
    def _experiment_cleanup(tool: ChaosClient, model: JujuModelHandle, unit: str, path: str = "") -> _CleanupAction:
        return _CleanupAction((model.uri, unit), path, lambda: tool.cleanup(model, unit, path))

    def _dispatch(
        self,
        operation: str,
        invoke: Callable[[ChaosClient], None],
        cleanup: Callable[[ChaosClient], _CleanupAction],
        pending: list[_CleanupAction],
    ) -> None:
        for tool in self._tools:
            action = cleanup(tool)
            try:
                invoke(tool)
            except NotImplementedError:
                continue
            except BaseException:
                # Failed calls may have created resources.
                pending.append(action)
                raise
            pending.append(action)
            return
        self._on_unsupported(operation)

    @staticmethod
    def _cleanup(pending: list[_CleanupAction], scope: tuple[str, str] | None, path: str) -> None:
        errors: list[Exception] = []
        for action in reversed(tuple(pending)):
            if scope is not None and (action.scope != scope or action.path != path):
                continue
            try:
                action.run()
            except Exception as error:
                # Keep failed actions for retry and continue cleanup.
                errors.append(error)
            else:
                pending.remove(action)
        if errors:
            raise ChaosCleanupError(errors) from errors[0]
