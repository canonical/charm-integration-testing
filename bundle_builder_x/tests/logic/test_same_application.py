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

"""Logic tests for same-application constraints.

Covers Section 7 of charm-deployment-constraints.rst:

When a charm integrates multiple endpoints, some endpoint pairs must connect to
the same remote application instance.

Real example: mongodb-k8s has ``ldap`` and ``ldap-certificate-transfer``
endpoints.  When it integrates with glauth-k8s for LDAP, both endpoints must
go to the SAME glauth-k8s instance, because the certificate must correspond to
the LDAP service being used.

This is expressed as a DSL implies constraint:

    bool(endpoint[ldap]) == bool(endpoint[ldap-certificate-transfer])
    bool(endpoint[ldap-certificate-transfer]) => reachable(endpoint[ldap-certificate-transfer]) >= charms(endpoint[ldap])

The second (reachable) constraint is complex and requires proxy chain support;
here we focus on the simpler equality and implication forms.
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestSameApplicationConstraints:
    """Section 7: Endpoint pairs that must connect to the same application."""

    def test_ldap_implies_certificate_transfer_is_added(self) -> None:
        # GIVEN mongodb-k8s with:
        #   - ldap: optional requires endpoint for LDAP authentication
        #   - ldap-certificate-transfer: optional requires endpoint for LDAP certs
        #   - constraint: if ldap is connected, cert-transfer must also be connected
        mongodb = make_charm(
            "mongodb-k8s",
            endpoints={
                "ldap": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="ldap",
                    optional=False,  # for this test: non-optional forces glauth to be added
                ),
                "ldap-certificate-transfer": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="certificate_transfer",
                    optional=True,
                ),
            },
            constraint_strs=[
                "bool(endpoint[ldap]) => bool(endpoint[ldap-certificate-transfer])",
            ],
        )
        # AND glauth-k8s provides both ldap and certificate_transfer interfaces
        glauth = make_charm(
            "glauth-k8s",
            endpoints={
                "ldap": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="ldap",
                    optional=True,
                ),
                "send-ca-cert": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="certificate_transfer",
                    optional=True,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb, glauth))

        # WHEN building with only mongodb (ldap is non-optional -> glauth is added)
        bundle = build_single_model(
            builder,
            applications={"mongo": AppSpec(charm="mongodb-k8s")},
        )

        # THEN glauth is added (to satisfy ldap)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "glauth-k8s" in charm_names

        # AND the ldap-certificate-transfer endpoint is also integrated
        # (because bool(ldap) => bool(ldap-certificate-transfer))
        cert_integration = any(
            any(ep.endpoint == "ldap-certificate-transfer" for ep in integration) for integration in bundle.integrations
        )
        assert cert_integration, "ldap-certificate-transfer should be integrated when ldap is active"

    def test_equality_constraint_binds_both_endpoints_together(self) -> None:
        # GIVEN a charm where ldap and ldap-cert-transfer must BOTH be active
        # or BOTH be inactive (modelled as bool(A) == bool(B))
        mongodb = make_charm(
            "mongodb-k8s",
            endpoints={
                "ldap": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="ldap",
                    optional=True,
                ),
                "ldap-certificate-transfer": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="certificate_transfer",
                    optional=True,
                ),
            },
            constraint_strs=[
                "bool(endpoint[ldap]) == bool(endpoint[ldap-certificate-transfer])",
            ],
        )
        # AND glauth providing both
        glauth = make_charm(
            "glauth-k8s",
            endpoints={
                "ldap": CharmEndpoint(type=EndpointType.PROVIDES, interface="ldap", optional=True),
                "send-ca-cert": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="certificate_transfer", optional=True
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb, glauth))

        # WHEN building with an explicit ldap integration in the spec
        bundle = build_single_model(
            builder,
            applications={
                "mongo": AppSpec(charm="mongodb-k8s"),
                "glauth": AppSpec(charm="glauth-k8s"),
            },
            integrations=[
                IntegrationSpec(
                    application="mongo",
                    endpoint="ldap",
                    remote_application="glauth",
                    remote_endpoint="ldap",
                )
            ],
        )

        # THEN the equality constraint forces ldap-certificate-transfer to also be integrated
        cert_integration = any(any(ep.endpoint == "ldap-certificate-transfer" for ep in i) for i in bundle.integrations)
        assert cert_integration, "Equality bool(ldap) == bool(ldap-cert-transfer) must force cert-transfer active"

    def test_neither_endpoint_satisfied_is_also_valid(self) -> None:
        # GIVEN the same equality constraint (both or neither)
        mongodb = make_charm(
            "mongodb-k8s",
            endpoints={
                "ldap": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="ldap",
                    optional=True,
                ),
                "ldap-certificate-transfer": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="certificate_transfer",
                    optional=True,
                ),
            },
            constraint_strs=[
                "bool(endpoint[ldap]) == bool(endpoint[ldap-certificate-transfer])",
            ],
        )
        # AND no providers in the registry
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN building with only mongodb
        bundle = build_single_model(
            builder,
            applications={"mongo": AppSpec(charm="mongodb-k8s")},
        )

        # THEN the bundle is valid: both endpoints inactive satisfies bool(A) == bool(B)
        assert len(bundle.applications) == 1
        assert len(bundle.integrations) == 0
