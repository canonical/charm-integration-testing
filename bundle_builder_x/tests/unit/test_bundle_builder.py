# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Unit tests for bundle_builder.py."""

from unittest.mock import MagicMock

from bundle_builder_x.assertion_tags import SubordinateBaseMismatchTag
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint], ubuntu_version: str = "22.04") -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version=ubuntu_version,
        ubuntu_arch="amd64",
        endpoints=endpoints,
    )


def _domain_with_base_mismatch() -> Domain:
    domain = Domain()
    domain.models[ModelRef(name="m")] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        applications={
            "ubuntu": DomainApplication(charm="ubuntu"),
            "nrpe": DomainApplication(charm="nrpe"),
        },
    )
    add_charm_to_domain(
        _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
            ubuntu_version="22.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    add_charm_to_domain(
        _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
            ubuntu_version="24.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    return domain


def _mismatch_tag() -> SubordinateBaseMismatchTag:
    return SubordinateBaseMismatchTag(
        subordinate_charm_name="nrpe",
        subordinate_charm_id=1,
        subordinate_endpoint="general-info",
        principal_charm_name="ubuntu",
        principal_charm_id=0,
        principal_endpoint="juju-info",
        subordinate_base="24.04",
        principal_base="22.04",
    )


class TestHandleSubordinateBaseMismatch:
    """BundleBuilder._handle_subordinate_base_mismatch."""

    def test_returns_true_and_expands_when_subordinate_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        nrpe_2204 = _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")},
            ubuntu_version="22.04",
        )
        mock_charmhub = MagicMock(spec=CharmhubClient)
        mock_charmhub.charm_from_store.side_effect = [
            nrpe_2204,
            CharmReleaseNotFoundException("ubuntu", "No release on 24.04"),
        ]
        builder = BundleBuilder(charmhub_client=mock_charmhub)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "nrpe"
        assert domain.charms[2].spec.ubuntu_version == "22.04"

    def test_returns_true_and_expands_when_principal_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        ubuntu_2404 = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
            ubuntu_version="24.04",
        )
        mock_charmhub = MagicMock(spec=CharmhubClient)
        mock_charmhub.charm_from_store.side_effect = [
            CharmReleaseNotFoundException("nrpe", "No release on 22.04"),
            ubuntu_2404,
        ]
        builder = BundleBuilder(charmhub_client=mock_charmhub)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "ubuntu"
        assert domain.charms[2].spec.ubuntu_version == "24.04"

    def test_returns_false_when_no_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        mock_charmhub = MagicMock(spec=CharmhubClient)
        mock_charmhub.charm_from_store.side_effect = CharmReleaseNotFoundException("nrpe", "No release")
        builder = BundleBuilder(charmhub_client=mock_charmhub)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is False
        assert len(domain.charms) == 2
