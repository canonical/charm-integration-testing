# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock

from juju import JujuApplicationInfo, JujuModelHandle
from juju.models import CharmChannel as JujuCharmChannel
from test_suite.fixtures.integration_spec import _resolve_deployed_charm

from bundle_builder_x import Charm, CharmChannel


class TestResolveDeployedCharmBase:
    """``_resolve_deployed_charm`` must pass the deployed application's Ubuntu base through to
    ``CharmhubClient.charm_from_store`` so base-scoped overrides resolve for the actual deployed
    base, rather than ``CharmhubClient`` silently picking the first base a revision supports.
    """

    def _application_info(self, base: str | None, branch: str = "") -> JujuApplicationInfo:
        return JujuApplicationInfo(
            charm="my-charm",
            revision=1,
            channel=JujuCharmChannel(track="1.0", risk="stable", branch=branch),
            base=base,
        )

    def test_passes_deployed_base_to_charm_from_store(self) -> None:
        # GIVEN an application deployed on a known Ubuntu base
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {"my-app": self._application_info(base="22.04")}
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm for that application
        _resolve_deployed_charm(charmhub_client, juju_client, model_ref, "my-app", cache={}, arch="amd64")

        # THEN the resolved base is threaded through as ubuntu_version
        charmhub_client.charm_from_store.assert_called_once()
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_version"] == "22.04"
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_arch"] == "amd64"

    def test_passes_deployed_channel_branch_to_charm_from_store(self) -> None:
        # GIVEN an application deployed from a channel branch
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {"my-app": self._application_info(base="22.04", branch="feature")}
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm for that application
        _resolve_deployed_charm(charmhub_client, juju_client, model_ref, "my-app", cache={}, arch="amd64")

        # THEN Charmhub receives the branch along with the other channel selectors
        assert charmhub_client.charm_from_store.call_args.kwargs["charm_branch"] == "feature"

    def test_passes_none_when_base_is_unknown(self) -> None:
        # GIVEN an application with no resolvable base
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {"my-app": self._application_info(base=None)}
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm for that application
        _resolve_deployed_charm(charmhub_client, juju_client, model_ref, "my-app", cache={}, arch="arm64")

        # THEN ubuntu_version is explicitly None rather than omitted
        charmhub_client.charm_from_store.assert_called_once()
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_version"] is None
        assert charmhub_client.charm_from_store.call_args.kwargs["ubuntu_arch"] == "arm64"

    def test_returns_local_charm_for_local_origin_application(self) -> None:
        # GIVEN an application deployed from a local charm artifact
        model_ref = JujuModelHandle(model="my-model", controller="my-controller")
        local_charm = Charm(
            name="my-charm",
            source_path=Path("/charms/my-charm"),
            channel=CharmChannel.model_validate("latest/stable"),
            revision=1,
            ubuntu_version="26.04",
            ubuntu_arch="amd64",
            endpoints={},
            platforms=["machine"],
        )
        juju_client = MagicMock()
        juju_client.list_applications.return_value = {
            "my-app": JujuApplicationInfo(charm="local:my-charm-0", revision=0, origin="local", base="26.04")
        }
        charmhub_client = MagicMock()

        # WHEN resolving the deployed charm
        deployed_charm = _resolve_deployed_charm(
            charmhub_client,
            juju_client,
            model_ref,
            "my-app",
            cache={},
            arch="amd64",
            local_charm=local_charm,
        )

        # THEN the local artifact metadata is used instead of querying Charmhub
        assert deployed_charm is local_charm
        charmhub_client.charm_from_store.assert_not_called()
