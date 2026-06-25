# Copyright (C) 2026 Canonical Ltd
# See LICENSE file for licensing details.

"""Shared test helper: materialize all-pairs charm integrations for a domain.

Production code creates integration variables lazily (CEGIS on integrations) via
``get_or_create_integration``, driven by the solver loop. Constraint-, extraction-
and domain-level unit tests, however, want a *fully wired* domain - the state the
solver would reach after complete expansion - without driving the loop.

``materialize_all_integrations`` reproduces exactly the pairing semantics the old
eager ``add_charm_to_domain`` had (compatible interface, opposite endpoint type,
container-scope co-location gating), so those tests keep asserting the same domain
shape they always did.
"""

from bundle_builder_x.charm import EndpointScope, EndpointType
from bundle_builder_x.domain import Domain, get_or_create_integration


def materialize_all_integrations(domain: Domain) -> None:
    """Create every compatible charm-integration variable for an already-built domain."""
    n = len(domain.charms)
    for a in range(n):
        charm_a = domain.charms[a]
        for b in range(n):
            if a == b:
                continue
            charm_b = domain.charms[b]
            same_model = charm_a.model == charm_b.model
            for ep_a_name, ep_a in charm_a.spec.endpoints.items():
                for ep_b_name, ep_b in charm_b.spec.endpoints.items():
                    if ep_a.interface != ep_b.interface:
                        continue
                    if ep_a.type == EndpointType.REQUIRES and ep_b.type == EndpointType.PROVIDES:
                        req_c, req_e, prov_c, prov_e = a, ep_a_name, b, ep_b_name
                    elif ep_a.type == EndpointType.PROVIDES and ep_b.type == EndpointType.REQUIRES:
                        req_c, req_e, prov_c, prov_e = b, ep_b_name, a, ep_a_name
                    else:
                        continue
                    if ep_a.scope == EndpointScope.CONTAINER or ep_b.scope == EndpointScope.CONTAINER:
                        if not same_model:
                            continue
                        req_plat = domain.models[domain.charms[req_c].model].platform
                        prov_plat = domain.models[domain.charms[prov_c].model].platform
                        if req_plat != "machine" or prov_plat != "machine":
                            continue
                    get_or_create_integration(domain, req_c, req_e, prov_c, prov_e)
