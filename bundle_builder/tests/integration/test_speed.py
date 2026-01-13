# Copyright (C) 2025 Canonical Ltd

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

from datetime import timedelta

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder import Application, Bundle, BundleBuilder, CharmhubClient


@dataclass
class Params:
    charms: set[str]
    platform: str
    arch: str


@pytest.mark.parametrize(
    "params",
    [
        Params(charms={"grafana-agent-k8s"}, platform="kubernetes", arch="amd64"),
        Params(charms={"istio-gateway", "kfp-ui"}, platform="kubernetes", arch="amd64"),
        Params(
            charms={
                "istio-gateway",
                "vault-k8s",
                "grafana-agent-k8s",
                "grafana-k8s",
                "mysql-k8s",
                "postgresql-k8s",
                "pgbouncer-k8s",
                "kratos",
                "hydra",
                "kfp-ui",
                "kafka-k8s",
            },
            platform="kubernetes",
            arch="amd64",
        ),
    ],
)
@pytest.mark.timeout(timedelta(minutes=20).total_seconds())
def test_speed(charmhub_client: CharmhubClient, params: Params) -> None:
    # GIVEN a base bundle
    base_bundle = Bundle(
        applications=frozenset(
            {Application(charm, charmhub_client.charm_from_store(charm, params.arch)) for charm in params.charms}
        ),
        integrations=frozenset(),
        platform=params.platform,
        arch=params.arch,
    )

    # WHEN minimal bundle is built
    minimal_bundle = BundleBuilder(charmhub_client).build(base_bundle)

    # THEN the test doesn't timeout in 20 minutes
    # AND the minimal bundle contains all the charms
    assert params.charms <= {application.name for application in minimal_bundle.applications}
