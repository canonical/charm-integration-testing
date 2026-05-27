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
from bundle_builder_x.domain import ModelRef
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

        # k8s-model has no controller so its key is the plain name;
        # machine-model has a controller so both the full key and a plain alias are present.
        assert "k8s-model" in spec.models_by_name
        assert "machine-model" in spec.models_by_name
        assert "lxd-controller/machine-model" in spec.models_by_name

        # k8s-model has one local and one in-spec CMR: both appear as integration constraints
        integrations_k8s = classify_integrations(
            spec.models_by_name["k8s-model"],
            spec.models_by_name,
        )
        assert len(integrations_k8s) == 2

        local_only = [i for i in integrations_k8s if i.endpoint_1.model == i.endpoint_2.model]
        assert len(local_only) == 1
        ic = local_only[0]
        assert {ic.endpoint_1.application, ic.endpoint_2.application} == {"webapp", "db-proxy"}

        cmr_ints = [i for i in integrations_k8s if i.offer_name is not None]
        assert len(cmr_ints) == 1
        cmr_int = cmr_ints[0]
        # Identify the remote endpoint (the one with a model set)
        remote_ep = cmr_int.endpoint_1 if cmr_int.endpoint_1.model != ModelRef() else cmr_int.endpoint_2
        local_ep = cmr_int.endpoint_2 if cmr_int.endpoint_1.model != ModelRef() else cmr_int.endpoint_1
        assert local_ep.application == "db-proxy"
        assert local_ep.endpoint == "backend-database"
        # The CMR references machine-model by plain name; classify_integrations resolves
        # it to a ModelRef with controller and name for consistent domain lookups.
        assert remote_ep.model == ModelRef(name="machine-model", controller="lxd-controller")
        assert remote_ep.application == "postgresql"
        assert cmr_int.offer_name == "postgresql-db-offer"
        assert cmr_int.url is None  # URL synthesis happens in extract.py once endpoint role is known

        # machine-model has one external CMR: produces one integration constraint
        integrations_m = classify_integrations(
            spec.models_by_name["machine-model"],
            spec.models_by_name,
        )
        assert len(integrations_m) == 1
        ext_cmr = next(iter(integrations_m))
        assert ext_cmr.url == "cos:admin/monitoring.prometheus-scrape"

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
        classify_integrations(spec.models_by_name["k8s-model"], spec.models_by_name)
        classify_integrations(spec.models_by_name["machine-model"], spec.models_by_name)

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

        # Verify machine-model exported YAML (offers are in the overlay document)
        machine_docs = list(yaml.safe_load_all(machine_bundle.export()))
        machine_overlay = machine_docs[1]
        pg_app = machine_overlay["applications"]["postgresql"]
        assert "offers" in pg_app
        assert "postgresql-db-offer" in pg_app["offers"]
        assert pg_app["offers"]["postgresql-db-offer"]["endpoints"] == ["database"]
        assert "prometheus-scrape" in pg_app["offers"]
        assert pg_app["offers"]["prometheus-scrape"]["endpoints"] == ["metrics-endpoint"]
