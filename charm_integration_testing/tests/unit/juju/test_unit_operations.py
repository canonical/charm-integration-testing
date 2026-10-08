# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from juju import JujuClient, JujuModelHandle

from ..extensions.shared import NullJujuBackend

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class UnitOperationsBackend(NullJujuBackend):
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def application_leader(self, model: JujuModelHandle, application: str) -> str:
        self.calls.append(("application_leader", model, application))
        return f"{application}/1"

    def remove_unit(self, model: JujuModelHandle, unit: str) -> None:
        self.calls.append(("remove_unit", model, unit))

    def wait_for_unit_removal(self, model: JujuModelHandle, unit: str, timeout: timedelta | None) -> None:
        self.calls.append(("wait_for_unit_removal", model, unit, timeout))

    def ssh(self, model: JujuModelHandle, application: str, command: str) -> None:
        self.calls.append(("ssh", model, application, command))


def test_unit_operations_delegate_to_backend() -> None:
    backend = UnitOperationsBackend()
    client = JujuClient(backend=backend, logger=logging.getLogger(__name__))
    timeout = timedelta(minutes=15)

    assert client.application_leader("target", model=MODEL) == "target/1"
    client.remove_unit("target/1", model=MODEL)
    client.wait_for_unit_removal("target/1", model=MODEL, timeout=timeout)
    client.ssh("target/1", "sudo reboot", model=MODEL)

    assert backend.calls == [
        ("application_leader", MODEL, "target"),
        ("remove_unit", MODEL, "target/1"),
        ("wait_for_unit_removal", MODEL, "target/1", timeout),
        ("ssh", MODEL, "target/1", "sudo reboot"),
    ]
