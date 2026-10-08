# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from juju import JujuModelHandle, JujuWaitState
from juju_cmd import JujuCmdBackend
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class UnitOperationsClient:
    units: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def model(self, model: JujuModelHandle) -> "UnitOperationsClient":
        return self

    def status(self) -> Any:
        return SimpleNamespace(apps={"target": SimpleNamespace(units=self.units)})

    def cli(self, *args: str) -> str:
        self.calls.append(args)
        return ""


def test_application_leader_returns_unit_marked_as_leader() -> None:
    client = UnitOperationsClient(
        units={
            "target/0": SimpleNamespace(leader=False),
            "target/1": SimpleNamespace(leader=True),
        }
    )

    backend = JubilantBackend(client=cast(JubilantClient, client))
    assert backend.application_leader(MODEL, "target") == "target/1"


def test_application_leader_raises_when_no_leader_is_reported() -> None:
    client = UnitOperationsClient(units={"target/0": SimpleNamespace(leader=False)})

    with pytest.raises(RuntimeError, match="Expected one leader"):
        JubilantBackend(client=cast(JubilantClient, client)).application_leader(MODEL, "target")


def test_remove_unit_delegates_to_juju_cli() -> None:
    client = UnitOperationsClient()

    JubilantBackend(client=cast(JubilantClient, client)).remove_unit(MODEL, "target/1")

    assert client.calls == [("remove-unit", "--no-prompt", "target/1")]


def test_unit_operations_do_not_add_abstract_requirements_to_legacy_backend() -> None:
    assert not {
        "application_leader",
        "remove_unit",
        "wait_for_unit_removal",
        "wait_for_unit_unavailable",
    }.intersection(JujuCmdBackend.__abstractmethods__)


def test_wait_for_unit_removal_checks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = UnitOperationsClient(units={"target/0": SimpleNamespace(leader=False)})
    backend = JubilantBackend(client=cast(JubilantClient, client))
    ready_checks: list[tuple[bool, JujuWaitState]] = []

    def record_wait(model: JujuModelHandle, ready: Any, timeout: Any = None) -> None:
        ready_checks.append(ready(client.status()))

    monkeypatch.setattr(backend, "wait", record_wait)
    backend.wait_for_unit_removal(MODEL, "target/1", timeout=None)

    assert len(ready_checks) == 1
    assert ready_checks[0][0] is True
    assert ready_checks[0][1].message == "waiting for removal of unit 'target/1'"


@pytest.mark.parametrize(("agent_status", "expected"), [("down", True), ("lost", True), ("idle", False)])
def test_wait_for_unit_unavailable_checks_agent_status(
    monkeypatch: pytest.MonkeyPatch, agent_status: str, expected: bool
) -> None:
    client = UnitOperationsClient(
        units={"target/1": SimpleNamespace(juju_status=SimpleNamespace(current=agent_status))}
    )
    backend = JubilantBackend(client=cast(JubilantClient, client))
    ready_checks: list[tuple[bool, JujuWaitState]] = []

    def record_wait(model: JujuModelHandle, ready: Any, timeout: Any = None) -> None:
        ready_checks.append(ready(client.status()))

    monkeypatch.setattr(backend, "wait", record_wait)
    backend.wait_for_unit_unavailable(MODEL, "target/1", timeout=None)

    assert ready_checks[0][0] is expected
    assert ready_checks[0][1].message == "waiting for unit 'target/1' to become unavailable"
