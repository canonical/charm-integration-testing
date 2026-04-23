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

"""Logic tests for conditionally required endpoints.

Covers Section 4 of charm-deployment-constraints.rst:

An endpoint that is optional by default can become required when a condition is
met - typically when another endpoint is integrated.  This is expressed as an
implies constraint in the DSL:

    bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])

Real example: vault-k8s - when ``vault-pki`` is used (vault acts as
an intermediate CA), it must also have a ``tls-certificates-pki`` integration
with a parent CA (such as self-signed-certificates).
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.domain import ApplicationConstraint, IntegrationConstraint

from .conftest import JUJU, CharmhubClientStub, build_single_model, make_charm


class TestConditionalRequirements:
    """Section 4: Conditionally required endpoints via implies constraints."""

    def test_optional_endpoint_becomes_required_when_condition_met(self) -> None:
        # GIVEN vault-k8s with:
        #   - vault-pki: provides PKI certificates to downstream consumers (optional)
        #   - tls-certificates-pki: requires a parent CA for vault's own PKI (optional)
        #   - constraint: if vault-pki is used, tls-certificates-pki becomes required
        vault = make_charm(
            "vault-k8s",
            endpoints={
                "vault-pki": CharmEndpoint(type=EndpointType.PROVIDES, interface="vault_pki", optional=True),
                "tls-certificates-pki": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="tls_certificates", optional=True
                ),
            },
            constraint_strs=[
                "bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])",
            ],
        )
        # AND a PKI consumer that forces vault-pki to be integrated
        pki_consumer = make_charm(
            "pki-consumer",
            endpoints={
                "vault-pki": CharmEndpoint(type=EndpointType.REQUIRES, interface="vault_pki", optional=False),
            },
        )
        # AND self-signed-certificates as the available parent CA
        ssc = make_charm(
            "self-signed-certificates",
            endpoints={
                "certificates": CharmEndpoint(type=EndpointType.PROVIDES, interface="tls_certificates", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(vault, pki_consumer, ssc))

        # WHEN building vault + pki-consumer with an explicit vault-pki integration
        bundle = build_single_model(
            builder,
            applications={
                "vault": ApplicationConstraint(charm="vault-k8s"),
                "consumer": ApplicationConstraint(charm="pki-consumer"),
            },
            integrations={
                IntegrationConstraint(
                    application_1="vault",
                    endpoint_1="vault-pki",
                    application_2="consumer",
                    endpoint_2="vault-pki",
                )
            },
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN the solver adds self-signed-certificates to satisfy the conditional
        # tls-certificates-pki requirement (vault-pki is active -> tls-certs is required)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "self-signed-certificates" in charm_names

        # AND the tls-certificates integration exists
        assert any(
            any(ep.endpoint == "tls-certificates-pki" for ep in integration) for integration in bundle.integrations
        )

    def test_optional_endpoint_stays_optional_when_condition_not_met(self) -> None:
        # GIVEN the same vault-k8s charm
        vault = make_charm(
            "vault-k8s",
            endpoints={
                "vault-pki": CharmEndpoint(type=EndpointType.PROVIDES, interface="vault_pki", optional=True),
                "tls-certificates-pki": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="tls_certificates", optional=True
                ),
                # vault-kv: another optional provides endpoint, not relevant here
                "vault-kv": CharmEndpoint(type=EndpointType.PROVIDES, interface="vault_kv", optional=True),
            },
            constraint_strs=[
                "bool(endpoint[vault-pki]) => bool(endpoint[tls-certificates-pki])",
            ],
        )
        # AND self-signed-certificates is in the registry (but should NOT be added)
        ssc = make_charm(
            "self-signed-certificates",
            endpoints={
                "certificates": CharmEndpoint(type=EndpointType.PROVIDES, interface="tls_certificates", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(vault, ssc))

        # WHEN building vault alone (no vault-pki consumer, so vault-pki is NOT integrated)
        bundle = build_single_model(
            builder,
            applications={"vault": ApplicationConstraint(charm="vault-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN self-signed-certificates is NOT added (condition not triggered)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "self-signed-certificates" not in charm_names

        # AND there are no integrations (all endpoints are optional and unused)
        assert len(bundle.integrations) == 0

    def test_implies_chain_expands_transitively(self) -> None:
        # GIVEN a charm with a two-level implies chain:
        #   - endpoint A active => endpoint B required
        #   - endpoint B active => endpoint C required
        chain_charm = make_charm(
            "chain-app",
            endpoints={
                "ep-a": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_a", optional=True),
                "ep-b": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_b", optional=True),
                "ep-c": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_c", optional=True),
            },
            constraint_strs=[
                "bool(endpoint[ep-a]) => bool(endpoint[ep-b])",
                "bool(endpoint[ep-b]) => bool(endpoint[ep-c])",
            ],
        )
        # AND a consumer that forces ep-a to be integrated
        a_consumer = make_charm(
            "a-consumer",
            endpoints={
                "ep-a": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_a", optional=False),
            },
        )
        # AND providers for ep-b and ep-c
        b_provider = make_charm(
            "b-provider",
            endpoints={
                "ep-b": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_b", optional=True),
            },
        )
        c_provider = make_charm(
            "c-provider",
            endpoints={
                "ep-c": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_c", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(chain_charm, a_consumer, b_provider, c_provider))

        # WHEN building chain-app + a-consumer (which forces ep-a active)
        bundle = build_single_model(
            builder,
            applications={
                "chain": ApplicationConstraint(charm="chain-app"),
                "consumer": ApplicationConstraint(charm="a-consumer"),
            },
            integrations={
                IntegrationConstraint(
                    application_1="chain",
                    endpoint_1="ep-a",
                    application_2="consumer",
                    endpoint_2="ep-a",
                )
            },
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN both b-provider and c-provider are added (full chain triggered)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "b-provider" in charm_names, "ep-a => ep-b should have triggered b-provider expansion"
        assert "c-provider" in charm_names, "ep-b => ep-c should have triggered c-provider expansion"
