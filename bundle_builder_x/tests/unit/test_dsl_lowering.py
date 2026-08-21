# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for lowering constraint DSL expressions to Z3."""

import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, CharmEndpointProxy, EndpointType
from bundle_builder_x.constraints_dsl import parse_constraint
from bundle_builder_x.domain import Domain, DomainModel, ModelRef, add_charm_to_domain, pair_charms_in_domain
from bundle_builder_x.dsl_lowering import LoweringContext, lower
from bundle_builder_x.juju_version import JujuVersion

_MODEL = ModelRef(name="testing")
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")


def _ast_node_count(expr: z3.ExprRef) -> int:
    seen: set[int] = set()
    pending = [expr]
    while pending:
        node = pending.pop()
        node_id = node.get_id()
        if node_id in seen:
            continue
        seen.add(node_id)
        pending.extend(node.children())
    return len(seen)


def _charm(
    name: str,
    endpoints: dict[str, CharmEndpoint],
    proxy: CharmEndpointProxy | None = None,
) -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints,
        proxies=[proxy] if proxy is not None else [],
        platforms=["kubernetes"],
    )


def test_reachable_set_converges_across_all_proxy_charms() -> None:
    # GIVEN a two-hop proxy chain between a CA provider and the target charm.
    domain = Domain(
        models={
            _MODEL: DomainModel(
                arch="amd64",
                platform="kubernetes",
                juju_version=JujuVersion(major=3, minor=6, patch=0),
            )
        }
    )
    client_id = add_charm_to_domain(
        _charm(
            "client",
            {
                "receive-ca": CharmEndpoint(type=EndpointType.REQUIRES, interface="root-ca"),
                "target": CharmEndpoint(type=EndpointType.REQUIRES, interface="service"),
            },
        ),
        domain,
        _MODEL,
    )
    root_id = add_charm_to_domain(
        _charm("root", {"ca": CharmEndpoint(type=EndpointType.PROVIDES, interface="root-ca")}),
        domain,
        _MODEL,
    )
    proxy_1_id = add_charm_to_domain(
        _charm(
            "proxy-1",
            {
                "upstream": CharmEndpoint(type=EndpointType.REQUIRES, interface="root-ca"),
                "downstream": CharmEndpoint(type=EndpointType.PROVIDES, interface="intermediate-ca"),
            },
            CharmEndpointProxy(interface="certificates", requires="upstream", provides="downstream"),
        ),
        domain,
        _MODEL,
    )
    proxy_2_id = add_charm_to_domain(
        _charm(
            "proxy-2",
            {
                "upstream": CharmEndpoint(type=EndpointType.REQUIRES, interface="intermediate-ca"),
                "service": CharmEndpoint(type=EndpointType.PROVIDES, interface="service"),
            },
            CharmEndpointProxy(interface="certificates", requires="upstream", provides="service"),
        ),
        domain,
        _MODEL,
    )
    pair_charms_in_domain(domain, client_id, root_id)
    pair_charms_in_domain(domain, root_id, proxy_1_id)
    pair_charms_in_domain(domain, proxy_1_id, proxy_2_id)
    pair_charms_in_domain(domain, client_id, proxy_2_id)

    constraint = parse_constraint("reachable(endpoint[receive-ca]) >= charms(endpoint[target])")
    result = lower(
        constraint,
        LoweringContext(charm_id=client_id, domain_charm=domain.charms[client_id], domain=domain),
    )
    node_count = _ast_node_count(result.expr)

    # AND unrelated non-proxy charms are added to the domain.
    for index in range(10):
        add_charm_to_domain(_charm(f"unrelated-{index}", {}), domain, _MODEL)

    expanded_result = lower(
        constraint,
        LoweringContext(charm_id=client_id, domain_charm=domain.charms[client_id], domain=domain),
    )
    solver = z3.Solver()
    solver.add(*(integration.exists for integration in domain.charm_integrations))

    # WHEN solving the reachability constraint.
    solver.add(expanded_result.expr)

    # THEN the target is reachable through both proxy-capable charms.
    assert solver.check() == z3.sat

    # AND non-proxy charms do not increase the symbolic fixed-point depth.
    assert _ast_node_count(expanded_result.expr) == node_count
