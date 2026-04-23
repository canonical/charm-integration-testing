# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_MODEL, provides=State.EMPTY_MODEL, bridge_only=True)
def test_create_model(
    juju_client: JujuClient,
    # FIXME(@motjuste): we should just use all_bundles
    juju_controller: str,
    model: str,
    juju_model_config: dict[str, str],
    all_bundles: list[tuple[str, str]],
) -> None:
    if not len(all_bundles):
        all_bundles = [("unused", f"cloud:{juju_controller}:{model}")]

    for _, model_info in all_bundles:
        _, juju_controller, model = model_info.split(":")
        juju_client.add_model(controller=juju_controller, model=model, model_config=juju_model_config)
