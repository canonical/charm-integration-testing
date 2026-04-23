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

"""Logic tests for minimum observability constraints.

Covers Section 9 of charm-deployment-constraints.rst:

A charm must have at least N endpoints from a set of M observability endpoints
integrated.

Real example: grafana-agent-k8s must have at least one of
``metrics-endpoint``, ``logging-provider``, ``tracing-provider``, or
``grafana-dashboards-consumer`` active.  All are optional individually, but the
DSL constraint forces at least one to be present:

    bool(endpoint[metrics-endpoint]) or bool(endpoint[logging-provider])
    or bool(endpoint[tracing-provider]) or bool(endpoint[grafana-dashboards-consumer])

When the solver processes the charm and finds the constraint failing (all counts
are 0), Z3 generates an ENDPOINT_COUNT_MATCHES_INTEGRATIONS tag for one of the
endpoints.  The handler then expands the domain by adding a provider for that
endpoint, satisfying both the count constraint and the DSL constraint.
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.domain import ApplicationConstraint

from .conftest import JUJU, CharmhubClientStub, build_single_model, make_charm


class TestMinimumObservability:
    """Section 9: At-least-N observability constraints via DSL OR expressions."""

    def test_at_least_one_input_required_expands_domain(self) -> None:
        # GIVEN grafana-agent-k8s with four optional input endpoints and a
        # constraint that at least one must be active
        grafana_agent = make_charm(
            "grafana-agent-k8s",
            endpoints={
                "metrics-endpoint": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="prometheus_scrape",
                    optional=True,
                ),
                "logging-provider": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="loki_push",
                    optional=True,
                ),
                "send-remote-write": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="prometheus_remote_write",
                    optional=True,
                ),
            },
            constraint_strs=[
                # At least one input must be present
                "bool(endpoint[metrics-endpoint]) or bool(endpoint[logging-provider])",
                # If metrics is used, send-remote-write must also be present
                "bool(endpoint[metrics-endpoint]) => bool(endpoint[send-remote-write])",
            ],
        )
        # AND a prometheus charm providing metrics-endpoint
        prometheus = make_charm(
            "prometheus-k8s",
            endpoints={
                "metrics-endpoint": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="prometheus_scrape",
                    optional=True,
                ),
            },
        )
        # AND a remote-write target
        remote_write_target = make_charm(
            "grafana-cloud-config",
            endpoints={
                "remote-write": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="prometheus_remote_write",
                    optional=True,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(grafana_agent, prometheus, remote_write_target))

        # WHEN building with just grafana-agent
        bundle = build_single_model(
            builder,
            applications={"agent": ApplicationConstraint(charm="grafana-agent-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN the solver adds at least one input source to satisfy the or-constraint
        charm_names = {a.charm.name for a in bundle.applications.values()}
        inputs_added = charm_names & {"prometheus-k8s", "grafana-cloud-config"}
        assert inputs_added, "The 'at least one input' constraint should force at least one provider to be added"

        # AND if metrics-endpoint was satisfied, send-remote-write must also be satisfied
        metrics_integrated = any(any(ep.endpoint == "metrics-endpoint" for ep in i) for i in bundle.integrations)
        if metrics_integrated:
            remote_write_integrated = any(
                any(ep.endpoint == "send-remote-write" for ep in i) for i in bundle.integrations
            )
            assert (
                remote_write_integrated
            ), "metrics-endpoint => send-remote-write must be satisfied when metrics is active"

    def test_zero_providers_for_required_observability_raises(self) -> None:
        # GIVEN grafana-agent with the or-constraint but NO providers in the registry
        grafana_agent = make_charm(
            "grafana-agent-k8s",
            endpoints={
                "metrics-endpoint": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="prometheus_scrape",
                    optional=True,
                ),
                "logging-provider": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="loki_push",
                    optional=True,
                ),
            },
            constraint_strs=[
                "bool(endpoint[metrics-endpoint]) or bool(endpoint[logging-provider])",
            ],
        )
        # AND NO providers in the registry
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(grafana_agent))

        # WHEN building
        # THEN the solver cannot satisfy the or-constraint (no providers to expand to)
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"agent": ApplicationConstraint(charm="grafana-agent-k8s")},
                integrations=set(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
            )

    def test_multiple_or_options_satisfies_constraint_with_one_expansion(self) -> None:
        # GIVEN an observability aggregator with three optional inputs
        aggregator = make_charm(
            "aggregator",
            endpoints={
                "input-a": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_a", optional=True),
                "input-b": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_b", optional=True),
                "input-c": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface_c", optional=True),
            },
            constraint_strs=[
                "bool(endpoint[input-a]) or bool(endpoint[input-b]) or bool(endpoint[input-c])",
            ],
        )
        # AND separate providers for each input
        provider_a = make_charm(
            "provider-a",
            endpoints={"out-a": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_a", optional=True)},
            priority=10.0,  # preferred by the optimizer
        )
        provider_b = make_charm(
            "provider-b",
            endpoints={"out-b": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_b", optional=True)},
        )
        provider_c = make_charm(
            "provider-c",
            endpoints={"out-c": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface_c", optional=True)},
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(aggregator, provider_a, provider_b, provider_c))

        bundle = build_single_model(
            builder,
            applications={"agg": ApplicationConstraint(charm="aggregator")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN at least one provider was added (not necessarily all three)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        providers_added = charm_names & {"provider-a", "provider-b", "provider-c"}
        assert len(providers_added) >= 1

        # AND at least one integration exists
        assert len(bundle.integrations) >= 1
