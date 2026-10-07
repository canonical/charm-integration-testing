# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Callable, Literal, NoReturn

from juju import JujuBackend, JujuModelHandle

from bundle_builder_x import CharmChannel

from .backend import ChaosClient
from .resource_constraints import CharmResourceConstraints, ResourceConstraintsClient


class ChaosNotSupportedError(NotImplementedError):
    """No configured client supports the requested experiment."""


def _raise_unsupported(operation: str) -> NoReturn:
    raise ChaosNotSupportedError(f"No configured chaos client supports '{operation}'.")


class ChaosCleanupError(RuntimeError):
    """Raised when one or more cleanup operations fail."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"{len(errors)} chaos cleanup operation(s) failed.")


class ChaosResourceConstraintsError(RuntimeError):
    """Raised when a unit's chaos resource constraints cannot be resolved.

    Failing loudly here is deliberate: silently falling back to an all-default
    (unconstrained) block would let a charm's configured per-charm limits be
    bypassed whenever its metadata is temporarily unavailable.
    """


@dataclass(frozen=True)
class _CleanupAction:
    scope: tuple[str, str]
    path: str
    run: Callable[[], None]
    stress_tool: ChaosClient | None = None


class MetaChaosClient(ChaosClient):
    """Try clients in order for each experiment, falling back on NotImplementedError.

    Do not share this instance or its clients between tests.
    """

    def __init__(
        self,
        tools: list[ChaosClient],
        backend: JujuBackend,
        resource_constraints_client: ResourceConstraintsClient,
        *,
        on_unsupported: Callable[[str], NoReturn] = _raise_unsupported,
    ) -> None:
        self._tools = tuple(tools)
        self._backend = backend
        self._resource_constraints_client = resource_constraints_client
        self._on_unsupported = on_unsupported
        self._cleanups: list[_CleanupAction] = []
        self._network_cleanups: list[_CleanupAction] = []

    def supports(self, operation: str) -> bool:
        return any(tool.supports(operation) for tool in self._tools)

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        def build_invoke(constraints: CharmResourceConstraints) -> Callable[[ChaosClient], None]:
            merged_size_mb = self._merged_int(constraints.disk_fill_size_mb, size_mb)
            return lambda tool: tool.fill_disk(model, unit, path, merged_size_mb)

        self._dispatch_constrained(
            "fill_disk",
            model,
            unit,
            build_invoke,
            lambda tool: self._experiment_cleanup(tool, model, unit, path),
            self._cleanups,
        )

    def stress_cpu(
        self,
        model: JujuModelHandle,
        unit: str,
        workers: int,
        duration: timedelta,
        *,
        scenario: Literal["exhaustion", "moderate_pressure"] = "exhaustion",
        duration_margin: timedelta = timedelta(0),
    ) -> timedelta:
        """Inject CPU stress for the resolved duration plus margin; return the duration without margin."""
        if duration_margin < timedelta(0):
            raise ValueError("CPU stress duration margin must be nonnegative.")
        if not self.supports("stress_cpu"):
            self._on_unsupported("stress_cpu")
        constraints = self._resolve_constraints(model, unit)
        match scenario:
            case "exhaustion":
                merged_workers = self._merged_int(constraints.cpu_exhaustion_workers, workers)
                merged_duration = self._merged_duration(constraints.cpu_exhaustion_duration_seconds, duration)
            case "moderate_pressure":
                merged_workers = self._merged_int(constraints.cpu_moderate_pressure_workers, workers)
                merged_duration = self._merged_duration(constraints.cpu_moderate_pressure_duration_seconds, duration)
            case _:
                raise ValueError(f"Unsupported CPU stress scenario: {scenario!r}")

        self._dispatch(
            "stress_cpu",
            lambda tool: tool.stress_cpu(model, unit, merged_workers, merged_duration + duration_margin),
            lambda tool: self._experiment_cleanup(tool, model, unit),
            self._cleanups,
        )
        return merged_duration

    def check_stress(self, model: JujuModelHandle, unit: str, *, allow_completed: bool = False) -> None:
        """Check only successful stress owners, retaining them until cleanup succeeds."""
        tools = {
            id(action.stress_tool): action.stress_tool
            for action in self._cleanups
            if action.scope == (model.uri, unit) and action.stress_tool is not None
        }
        if not tools:
            raise RuntimeError(f"No successfully started stress for {model.uri}/{unit}.")
        for tool in tools.values():
            tool.check_stress(model, unit, allow_completed=allow_completed)

    def stress_memory(
        self,
        model: JujuModelHandle,
        unit: str,
        workers: int,
        size_mb: int,
        duration: timedelta,
        *,
        scenario: Literal["exhaustion", "moderate_pressure"] = "exhaustion",
    ) -> None:
        def build_invoke(constraints: CharmResourceConstraints) -> Callable[[ChaosClient], None]:
            match scenario:
                case "exhaustion":
                    merged_workers = self._merged_int(constraints.memory_exhaustion_workers, workers)
                    merged_size_mb = self._merged_int(constraints.memory_exhaustion_size_mb, size_mb)
                    merged_duration = self._merged_duration(constraints.memory_exhaustion_duration_seconds, duration)
                case "moderate_pressure":
                    merged_workers = self._merged_int(constraints.memory_moderate_pressure_workers, workers)
                    merged_size_mb = self._merged_int(constraints.memory_moderate_pressure_size_mb, size_mb)
                    merged_duration = self._merged_duration(
                        constraints.memory_moderate_pressure_duration_seconds, duration
                    )
                case _:
                    raise ValueError(f"Unsupported memory stress scenario: {scenario!r}")
            return lambda tool: tool.stress_memory(model, unit, merged_workers, merged_size_mb, merged_duration)

        self._dispatch_constrained(
            "stress_memory",
            model,
            unit,
            build_invoke,
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
        def build_invoke(constraints: CharmResourceConstraints) -> Callable[[ChaosClient], None]:
            merged_delay = self._merged_duration(constraints.disk_io_latency_delay_ms, delay, unit="milliseconds")
            merged_percent = self._merged_int(constraints.disk_io_latency_percent, percent)
            merged_duration = self._merged_duration(constraints.disk_io_latency_duration_seconds, duration)
            return lambda tool: tool.io_latency(model, unit, volume_path, merged_delay, merged_percent, merged_duration)

        self._dispatch_constrained(
            "io_latency",
            model,
            unit,
            build_invoke,
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

    def _resolve_constraints(self, model: JujuModelHandle, unit: str) -> CharmResourceConstraints:
        try:
            applications = self._backend.list_applications(model)
        except Exception as error:
            raise ChaosResourceConstraintsError(
                f"Could not list applications for model '{model.uri}' while resolving chaos resource "
                f"constraints for unit '{unit}'."
            ) from error

        application_name = unit.split("/", 1)[0]
        info = applications.get(application_name)
        if info is None or not info.charm or info.channel is None or info.base is None:
            raise ChaosResourceConstraintsError(
                f"Incomplete application metadata for unit '{unit}' in model '{model.uri}': "
                "cannot resolve chaos resource constraints without a known charm, channel and base."
            )

        channel = CharmChannel.model_validate(str(info.channel))
        return self._resource_constraints_client.get_charm_resource_constraints(info.charm, channel, info.base)

    @staticmethod
    def _merged_int(constraint_value: int | None, caller_value: int) -> int:
        return constraint_value if constraint_value is not None else caller_value

    @staticmethod
    def _merged_duration(
        constraint_seconds_or_ms: int | None,
        caller_value: timedelta,
        *,
        unit: Literal["seconds", "milliseconds"] = "seconds",
    ) -> timedelta:
        if constraint_seconds_or_ms is None:
            return caller_value
        if unit == "milliseconds":
            return timedelta(milliseconds=constraint_seconds_or_ms)
        return timedelta(seconds=constraint_seconds_or_ms)

    def _dispatch_constrained(
        self,
        operation: str,
        model: JujuModelHandle,
        unit: str,
        build_invoke: Callable[[CharmResourceConstraints], Callable[[ChaosClient], None]],
        cleanup: Callable[[ChaosClient], _CleanupAction],
        pending: list[_CleanupAction],
    ) -> None:
        """Resolve constraints only once a configured client could actually run the experiment.

        Checking ``ChaosClient.supports`` first (a side-effect-free, static capability
        check) avoids resolving constraints when no configured tool can run this
        operation at all, so an unsupported experiment is reported via on_unsupported
        instead of surfacing an unrelated resource-constraints lookup failure.
        """
        if not self.supports(operation):
            self._on_unsupported(operation)
            return
        invoke = build_invoke(self._resolve_constraints(model, unit))
        self._dispatch(operation, invoke, cleanup, pending)

    def _dispatch(
        self,
        operation: str,
        invoke: Callable[[ChaosClient], object],
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
            if operation in {"stress_cpu", "stress_memory"}:
                action = replace(action, stress_tool=tool)
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
