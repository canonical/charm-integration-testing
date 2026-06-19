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

"""Logic tests for transitive capabilities.

Covers Section 11 of charm-deployment-constraints.rst:

A capability required by a charm can be satisfied transitively through a chain
of integrations.  However, a charm may still need a DIRECT integration with the
root capability provider to verify the certificate chain.

Real example: juju-jimm-k8s requires:
  - oauth (from hydra)
  - receive-ca-cert (directly from self-signed-certificates)

The TLS trust chain flows: self-signed-certs -> traefik -> hydra -> jimm.
But jimm also needs a direct integration with self-signed-certs for
receive-ca-cert, so it can verify the certificates it receives through oauth.

This forces the solver to build the full dependency chain AND add the direct
self-signed-certs <-> jimm integration.
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestTransitiveCapabilities:
    """Section 11: Multi-hop dependency chains with direct root-provider requirements."""

    def test_full_dependency_chain_is_built(self) -> None:
        # GIVEN the JIMM dependency chain:
        #   jimm <- hydra <- traefik <- self-signed-certificates
        #   jimm also requires receive-ca-cert directly from self-signed-certificates

        jimm = make_charm(
            "juju-jimm-k8s",
            endpoints={
                "oauth": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="oauth",
                    optional=False,
                ),
                "receive-ca-cert": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="certificate_transfer",
                    optional=False,
                ),
            },
        )
        hydra = make_charm(
            "hydra",
            endpoints={
                "oauth": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="oauth",
                    optional=True,
                ),
                "public-ingress": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="ingress",
                    optional=False,
                ),
            },
        )
        traefik = make_charm(
            "traefik-k8s",
            endpoints={
                "ingress": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="ingress",
                    optional=True,
                ),
                "certificates": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="tls_certificates",
                    optional=False,
                ),
            },
        )
        # self-signed-certificates provides both TLS certificates AND certificate transfer
        ssc = make_charm(
            "self-signed-certificates",
            endpoints={
                "certificates": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="tls_certificates",
                    optional=True,
                ),
                "send-ca-cert": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="certificate_transfer",
                    optional=True,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(jimm, hydra, traefik, ssc))

        # WHEN building with only jimm
        bundle = build_single_model(
            builder,
            applications={"jimm": AppSpec(charm="juju-jimm-k8s")},
        )

        # THEN the full chain is built: hydra + traefik + self-signed-certs
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "hydra" in charm_names, "hydra must be added to satisfy jimm's oauth requirement"
        assert "traefik-k8s" in charm_names, "traefik must be added to satisfy hydra's ingress requirement"
        assert (
            "self-signed-certificates" in charm_names
        ), "self-signed-certs must be added for traefik's TLS AND jimm's receive-ca-cert"

    def test_direct_cert_requirement_is_satisfied_by_expanded_ssc(self) -> None:
        # GIVEN the same charm setup
        jimm = make_charm(
            "juju-jimm-k8s",
            endpoints={
                "oauth": CharmEndpoint(type=EndpointType.REQUIRES, interface="oauth", optional=False),
                "receive-ca-cert": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="certificate_transfer", optional=False
                ),
            },
        )
        hydra = make_charm(
            "hydra",
            endpoints={
                "oauth": CharmEndpoint(type=EndpointType.PROVIDES, interface="oauth", optional=True),
                "public-ingress": CharmEndpoint(type=EndpointType.REQUIRES, interface="ingress", optional=False),
            },
        )
        traefik = make_charm(
            "traefik-k8s",
            endpoints={
                "ingress": CharmEndpoint(type=EndpointType.PROVIDES, interface="ingress", optional=True),
                "certificates": CharmEndpoint(type=EndpointType.REQUIRES, interface="tls_certificates", optional=False),
            },
        )
        ssc = make_charm(
            "self-signed-certificates",
            endpoints={
                "certificates": CharmEndpoint(type=EndpointType.PROVIDES, interface="tls_certificates", optional=True),
                "send-ca-cert": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="certificate_transfer", optional=True
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(jimm, hydra, traefik, ssc))

        bundle = build_single_model(
            builder,
            applications={"jimm": AppSpec(charm="juju-jimm-k8s")},
        )

        # THEN there is a direct integration between jimm and self-signed-certs
        # for the receive-ca-cert endpoint
        cert_integration = any(any(ep.endpoint == "receive-ca-cert" for ep in i) for i in bundle.integrations)
        assert cert_integration, "jimm:receive-ca-cert must be directly integrated with self-signed-certificates"

        # AND the oauth integration to hydra exists
        oauth_integration = any(any(ep.endpoint == "oauth" for ep in i) for i in bundle.integrations)
        assert oauth_integration, "jimm:oauth must be integrated with hydra"

    def test_chain_depth_does_not_cause_cycles(self) -> None:
        # GIVEN a 4-hop dependency chain: A <- B <- C <- D
        # This tests that the solver correctly handles depth without false cycle detection
        charm_d = make_charm(
            "charm-d",
            endpoints={
                "provides-d": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_d", optional=True),
            },
        )
        charm_c = make_charm(
            "charm-c",
            endpoints={
                "requires-d": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_d", optional=False),
                "provides-c": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_c", optional=True),
            },
        )
        charm_b = make_charm(
            "charm-b",
            endpoints={
                "requires-c": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_c", optional=False),
                "provides-b": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_b", optional=True),
            },
        )
        charm_a = make_charm(
            "charm-a",
            endpoints={
                "requires-b": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_b", optional=False),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm_a, charm_b, charm_c, charm_d))

        bundle = build_single_model(
            builder,
            applications={"app-a": AppSpec(charm="charm-a")},
        )

        # THEN all four charms are in the bundle
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert charm_names == {"charm-a", "charm-b", "charm-c", "charm-d"}

        # AND the chain has 3 integrations (A->B, B->C, C->D)
        assert len(bundle.integrations) == 3
