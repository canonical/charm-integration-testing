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

"""End-to-end test for cross-model relation support.

Exercises the full pipeline: spec YAML -> parse -> classify -> bundle export
-> validation, using pre-built Bundle objects (no Charmhub calls).
"""

import yaml

from bundle_builder_x.bundle import (
    Application,
    ApplicationEndpoint,
    Bundle,
    CrossModelIntegration,
    Integration,
)
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.domain_builder import (
    applications_from_spec,
    classify_integrations,
)
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.spec import (
    SpecFile,
)


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint] | None = None) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel(track="1", risk="stable", branch=""),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
    )


SPEC_YAML = """\
models:
  - name: k8s-model
    arch: amd64
    platform: kubernetes
    juju: "3.6.1"
    applications:
      webapp:
        charm: my-webapp
      db-proxy:
        charm: pgbouncer-k8s
    integrations:
      - application: webapp
        endpoint: http
        remote_application: db-proxy
        remote_endpoint: http
      - application: db-proxy
        endpoint: backend-database
        remote_model: machine-model
        remote_application: postgresql
        remote_endpoint: database
        offer_name: postgresql-db-offer

  - name: machine-model
    arch: amd64
    platform: machine
    juju: "3.6.1"
    controller: lxd-controller
    admin: admin
    applications:
      postgresql:
        charm: postgresql
    integrations:
      - application: postgresql
        endpoint: metrics-endpoint
        remote_model: monitoring
        remote_application: prometheus
        remote_endpoint: metrics-endpoint
        url: "cos:admin/monitoring.prometheus-scrape"
"""


class TestCMREndToEnd:
    def test_spec_parses_and_classifies_correctly(self) -> None:
        """Validate spec parsing and integration classification for both models."""
        spec = SpecFile.model_validate(yaml.safe_load(SPEC_YAML))

        assert set(spec.models_by_name) == {"k8s-model", "machine-model"}

        # k8s-model has one local and one in-spec CMR
        local_k8s, cmr_k8s = classify_integrations(
            "k8s-model",
            spec.models_by_name["k8s-model"],
            spec.models_by_name,
        )
        assert len(local_k8s) == 1
        assert len(cmr_k8s) == 1

        ic = next(iter(local_k8s))
        assert {ic.application_1, ic.application_2} == {"webapp", "db-proxy"}

        cmr = cmr_k8s[0]
        assert cmr.local_application == "db-proxy"
        assert cmr.local_endpoint == "backend-database"
        assert cmr.remote.model == "machine-model"
        assert cmr.remote.application == "postgresql"
        assert cmr.remote.offer_name == "postgresql-db-offer"
        assert cmr.remote.url == "lxd-controller:admin/machine-model.postgresql-db-offer"

        # machine-model has one external CMR
        local_m, cmr_m = classify_integrations(
            "machine-model",
            spec.models_by_name["machine-model"],
            spec.models_by_name,
        )
        assert len(local_m) == 0
        assert len(cmr_m) == 1
        assert cmr_m[0].remote.url == "cos:admin/monitoring.prometheus-scrape"

    def test_applications_from_spec_for_both_models(self) -> None:
        """Verify all application constraints are produced."""
        spec = SpecFile.model_validate(yaml.safe_load(SPEC_YAML))

        k8s_apps = applications_from_spec(spec.models_by_name["k8s-model"])
        assert set(k8s_apps) == {"webapp", "db-proxy"}
        assert k8s_apps["webapp"].charm == "my-webapp"

        machine_apps = applications_from_spec(spec.models_by_name["machine-model"])
        assert set(machine_apps) == {"postgresql"}
        assert machine_apps["postgresql"].charm == "postgresql"

    def test_bundle_export_round_trip(self) -> None:
        """Build bundles from classified constraints and verify exported YAML structure."""
        spec = SpecFile.model_validate(yaml.safe_load(SPEC_YAML))
        _, cmr_k8s = classify_integrations("k8s-model", spec.models_by_name["k8s-model"], spec.models_by_name)
        _, cmr_m = classify_integrations("machine-model", spec.models_by_name["machine-model"], spec.models_by_name)

        # Build k8s-model bundle (the requires side of the CMR)
        k8s_bundle = Bundle(
            applications={
                "webapp": Application(
                    charm=_make_charm(
                        "my-webapp",
                        {
                            "http": CharmEndpoint(type=EndpointType.PROVIDES, interface="http"),
                        },
                    )
                ),
                "db-proxy": Application(
                    charm=_make_charm(
                        "pgbouncer-k8s",
                        {
                            "http": CharmEndpoint(type=EndpointType.REQUIRES, interface="http"),
                            "backend-database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql"),
                        },
                    )
                ),
            },
            integrations={
                Integration.create(
                    ApplicationEndpoint(application="webapp", endpoint="http"),
                    ApplicationEndpoint(application="db-proxy", endpoint="http"),
                ),
            },
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="db-proxy", endpoint="backend-database"),
                    local_role=EndpointType.REQUIRES,
                    remote_model="machine-model",
                    remote_application="postgresql",
                    remote_endpoint="database",
                    offer_name="postgresql-db-offer",
                    url="lxd-controller:admin/machine-model.postgresql-db-offer",
                ),
            ],
            platform="kubernetes",
            arch="amd64",
            juju_version=JujuVersion(major=3, minor=6, patch=0),
        )

        # Build machine-model bundle (the provides side for the in-spec CMR,
        # and the requires side for the external CMR)
        machine_bundle = Bundle(
            applications={
                "postgresql": Application(
                    charm=_make_charm(
                        "postgresql",
                        {
                            "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql"),
                            "metrics-endpoint": CharmEndpoint(
                                type=EndpointType.PROVIDES, interface="prometheus_scrape"
                            ),
                        },
                    )
                ),
            },
            integrations=set(),
            cross_model_integrations=[
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="database"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="k8s-model",
                    remote_application="db-proxy",
                    remote_endpoint="backend-database",
                    offer_name="postgresql-db-offer",
                ),
                CrossModelIntegration(
                    local=ApplicationEndpoint(application="postgresql", endpoint="metrics-endpoint"),
                    local_role=EndpointType.PROVIDES,
                    remote_model="monitoring",
                    remote_application="prometheus",
                    remote_endpoint="metrics-endpoint",
                    offer_name="prometheus-scrape",
                ),
            ],
            platform="machine",
            arch="amd64",
            juju_version=JujuVersion(major=3, minor=6, patch=0),
        )

        # Verify k8s-model exported YAML
        k8s_yaml = yaml.safe_load(k8s_bundle.export())
        assert "saas" in k8s_yaml
        assert "postgresql-db-offer" in k8s_yaml["saas"]
        assert (
            k8s_yaml["saas"]["postgresql-db-offer"]["url"] == "lxd-controller:admin/machine-model.postgresql-db-offer"
        )
        assert ["db-proxy:backend-database", "postgresql-db-offer:database"] in k8s_yaml["relations"]

        # Verify machine-model exported YAML
        machine_yaml = yaml.safe_load(machine_bundle.export())
        pg_app = machine_yaml["applications"]["postgresql"]
        assert "offers" in pg_app
        assert "postgresql-db-offer" in pg_app["offers"]
        assert pg_app["offers"]["postgresql-db-offer"]["endpoints"] == ["database"]
        assert "prometheus-scrape" in pg_app["offers"]
        assert pg_app["offers"]["prometheus-scrape"]["endpoints"] == ["metrics-endpoint"]
