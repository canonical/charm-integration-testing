# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

from juju import JujuApplicationInfo, JujuModelHandle
from juju.models import CharmChannel
from test_suite.fixtures.integration_spec import _resolve_deployed_charm


class TestResolveDeployedCharmBase:
    """``_resolve_deployed_charm`` must pass the deployed application's Ubuntu base through to
    ``CharmhubClient.charm_from_store`` so base-scoped overrides resolve for the actual deployed
    base, rather than ``CharmhubClient`` silently picking the first base a revision supports.
    """

    def _application_info(self, base: str | None) -> JujuApplicationInfo:
        return JujuApplicationInfo(
            charm="my-charm",
            revision=1,
            channel=CharmChannel(track="1.0", risk="stable", branch=""),
            base=base,
        )

    def test_passes_deployed_base_to_charm_from_store(self) -> None:
        # GIVEN an application deployed on a known Ubuntu base
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {"my-app": self._application_info(base="22.04")}
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm for that application
        _resolve_deployed_charm(charmhub_client, juju_client, model_ref, "my-app", cache={})

        # THEN the resolved base is threaded through as ubuntu_version
        charmhub_client.charm_from_store.assert_called_once()
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_version"] == "22.04"

    def test_passes_none_when_base_is_unknown(self) -> None:
        # GIVEN an application with no resolvable base
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {"my-app": self._application_info(base=None)}
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm for that application
        _resolve_deployed_charm(charmhub_client, juju_client, model_ref, "my-app", cache={})

        # THEN ubuntu_version is explicitly None rather than omitted
        charmhub_client.charm_from_store.assert_called_once()
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_version"] is None
