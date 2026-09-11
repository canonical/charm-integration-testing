# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the cross_model_mesh-specific solver constraints and offer grouping.

See issue #980: cross_model_mesh (provide-cmr-mesh/require-cmr-mesh) carries no application
traffic; it only makes sense alongside a genuine, already cross-model relation between the
same two charms, riding the same Juju offer.
"""

import logging

import pytest
import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.constraints_dsl import parse_constraint
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
    pair_charms_in_domain,
)
from bundle_builder_x.dsl_lowering import DSLLoweringError, LoweringContext, lower
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="1", risk="stable", branch="")


def _make_domain(models: dict[ModelRef, DomainModel]) -> Domain:
    domain = Domain()
    domain.models.update(models)
    return domain


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint]) -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints,
        platforms=["kubernetes"],
    )


def _mesh_pair_charms() -> tuple[Charm, Charm]:
    """A consumer charm requiring cmr-mesh + a real workload, and a provider charm providing both."""
    consumer = _make_charm(
        "consumer-app",
        {
            "require-cmr-mesh": CharmEndpoint(type=EndpointType.REQUIRES, interface="cross_model_mesh"),
            "backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload", optional=True),
        },
    )
    provider = _make_charm(
        "provider-app",
        {
            "provide-cmr-mesh": CharmEndpoint(type=EndpointType.PROVIDES, interface="cross_model_mesh"),
            "serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload", optional=True),
        },
    )
    return consumer, provider


class TestCrossModelExprDSL:
    def test_cross_model_true_only_for_cross_model_integration(self) -> None:
        # GIVEN two charms in different models, related on a plain workload endpoint
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)
        expr = parse_constraint("bool(cross_model(endpoint[backend]))")
        result = lower(expr, ctx)

        # bool(cross_model(x)) lowers to cross_model_count >= 1, and cross_model_count is linked
        # to actual integration existence via add_constraints (constraints.py), so it must be
        # present on both solvers.
        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(workload_integration.exists)
        assert solver.check(result.expr) == z3.sat

        solver2 = z3.Solver()
        add_constraints(solver2, domain)
        solver2.add(z3.Not(workload_integration.exists))
        assert solver2.check(result.expr) == z3.unsat

    def test_cross_model_over_unioned_relation_set_is_or_semantics(self) -> None:
        # GIVEN two charms in different models, related on two distinct real endpoints, using
        # the idiomatic `endpoint[a] | endpoint[b]` RelationSet-union form (matching the pattern
        # used in static/charm-overrides, e.g. traefik-k8s.yaml's features(endpoint[...] | ...))
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer = _make_charm(
            "consumer-app",
            {
                "require-cmr-mesh": CharmEndpoint(type=EndpointType.REQUIRES, interface="cross_model_mesh"),
                "backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload", optional=True),
                "backend-alt": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload_alt", optional=True),
            },
        )
        provider = _make_charm(
            "provider-app",
            {
                "provide-cmr-mesh": CharmEndpoint(type=EndpointType.PROVIDES, interface="cross_model_mesh"),
                "serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload", optional=True),
                "serve-alt": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload_alt", optional=True),
            },
        )
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]
        [workload_alt_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "workload_alt"
        ]

        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)
        expr = parse_constraint("bool(cross_model(endpoint[backend] | endpoint[backend-alt]))")
        result = lower(expr, ctx)

        # THEN True when EITHER underlying endpoint has a cross-model integration...
        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(z3.Not(workload_integration.exists))
        solver.add(workload_alt_integration.exists)
        assert solver.check(result.expr) == z3.sat

        # ...and False when neither does
        solver2 = z3.Solver()
        add_constraints(solver2, domain)
        solver2.add(z3.Not(workload_integration.exists))
        solver2.add(z3.Not(workload_alt_integration.exists))
        assert solver2.check(result.expr) == z3.unsat

    def test_len_cross_model_counts_only_cross_model_integrations(self) -> None:
        # GIVEN a consumer with an unlimited endpoint integrated with both a local peer
        # (same model) and a remote peer (different model)
        consumer = _make_charm(
            "consumer-app",
            {"backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload", limit=None)},
        )
        local_provider = _make_charm(
            "local-provider-app",
            {"serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload")},
        )
        remote_provider = _make_charm(
            "remote-provider-app",
            {"serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload")},
        )
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "consumer": DomainApplication(charm="consumer-app"),
                        "local-provider": DomainApplication(charm="local-provider-app"),
                    },
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"remote-provider": DomainApplication(charm="remote-provider-app")},
                ),
            }
        )
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        local_provider_id = add_charm_to_domain(local_provider, domain, ModelRef(name="model-a"))
        remote_provider_id = add_charm_to_domain(remote_provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, local_provider_id)
        pair_charms_in_domain(domain, consumer_id, remote_provider_id)

        [local_integration] = [
            i
            for i in domain.charm_integrations
            if (i.requires_charm_id == consumer_id and i.provides_charm_id == local_provider_id)
            or (i.provides_charm_id == consumer_id and i.requires_charm_id == local_provider_id)
        ]
        [remote_integration] = [
            i
            for i in domain.charm_integrations
            if (i.requires_charm_id == consumer_id and i.provides_charm_id == remote_provider_id)
            or (i.provides_charm_id == consumer_id and i.requires_charm_id == remote_provider_id)
        ]

        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)
        expr = parse_constraint("len(cross_model(endpoint[backend])) == 1")
        result = lower(expr, ctx)

        # THEN true when the remote (cross-model) integration is active, regardless of the local one...
        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(local_integration.exists)
        solver.add(remote_integration.exists)
        assert solver.check(result.expr) == z3.sat

        # ...but false if only the local integration is active (no cross-model contribution at all)
        solver2 = z3.Solver()
        add_constraints(solver2, domain)
        solver2.add(local_integration.exists)
        solver2.add(z3.Not(remote_integration.exists))
        assert solver2.check(result.expr) == z3.unsat

    def test_features_of_cross_model_endpoint_is_rejected(self) -> None:
        # GIVEN a plain endpoint reference wrapped in cross_model()
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
            }
        )
        consumer, _provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)

        # THEN features() on a cross_model()-tagged endpoint set fails loudly, since features
        # are declared per-endpoint (not per-relation-instance) and cross_model() filtering
        # has no well-defined meaning for them.
        expr = parse_constraint('features(cross_model(endpoint[backend])) == {"a"}')
        with pytest.raises(DSLLoweringError, match="features\\(\\)"):
            lower(expr, ctx)

    def test_mixing_filtered_and_unfiltered_same_endpoint_is_rejected(self) -> None:
        # GIVEN a RelationSet expression that unions a plain endpoint reference with a
        # cross_model()-filtered reference to the *same* endpoint name.
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
            }
        )
        consumer, _provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)

        # THEN lowering fails loudly instead of silently double-counting (len) or emptying (&),
        # since set operators distinguish cross_model()-filtered refs from unfiltered ones of the
        # same endpoint.
        expr = parse_constraint("len(endpoint[backend] | cross_model(endpoint[backend])) == 1")
        with pytest.raises(DSLLoweringError, match="both filtered and unfiltered"):
            lower(expr, ctx)

    @pytest.mark.parametrize("op", ["&", "-"])
    def test_mixing_filtered_and_unfiltered_same_endpoint_is_rejected_for_intersection_and_difference(
        self, op: str
    ) -> None:
        # GIVEN the same mixed expression as above but combined with "&" or "-" instead of "|".
        # Unlike "|" (which keeps both refs in its output, letting the post-hoc check see the
        # conflicting tags), "&"/"-" filter by _EndpointRef equality directly: "&" would otherwise
        # silently produce an empty set (wrongly evaluating len() to 0), and "-" would silently
        # keep the unfiltered endpoint's full count -- neither surfaces the conflict in its own
        # output, so it must be checked on the operands before combining.
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
            }
        )
        consumer, _provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        ctx = LoweringContext(charm_id=consumer_id, domain_charm=domain.charms[consumer_id], domain=domain)

        expr = parse_constraint(f"len(endpoint[backend] {op} cross_model(endpoint[backend])) == 1")
        with pytest.raises(DSLLoweringError, match="both filtered and unfiltered"):
            lower(expr, ctx)


class TestCrossModelMeshOfferSharing:
    def test_cross_model_pairing_shares_offer_with_companion_relation(self) -> None:
        # GIVEN two charms in different models, related via BOTH a real workload endpoint AND
        # cross_model_mesh. Offer-sharing is fully generic in domain.py: it applies to ANY two
        # cross-model integrations between the same charm pair, not just cross_model_mesh ones.
        domain = _make_domain(
            {
                ModelRef(name="model-a", controller="foo"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b", controller="foo"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a", controller="foo"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b", controller="foo"))
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)

        assert solver.check() == z3.sat
        model = solver.model()

        # THEN the mesh integration reuses the workload integration's offer name, so bundle.py's
        # grouping-by-offer-name logic puts both endpoints in the same Juju offer
        mesh_offer = domain.integration_offer_name(mesh_integration, model)
        workload_offer = domain.integration_offer_name(workload_integration, model)
        assert mesh_offer == workload_offer

        solution = extract_solution(model, domain, logging.getLogger("test"))
        provider_model_ref = ModelRef(name="model-b", controller="foo")
        [provider_bundle] = [b for b in solution.bundles if b.model == provider_model_ref.key]
        offer_names = {cmr.offer_name for cmr in provider_bundle.cross_model_integrations}
        assert offer_names == {workload_offer}

    def test_discovered_mesh_companion_reuses_explicit_user_cmr_offer_name(self) -> None:
        # GIVEN two charms in different models, with the user already declaring an explicit
        # (in-spec) CMR on the workload endpoint, under a custom offer name, and the solver
        # separately discovering the cross_model_mesh companion relation between the same pair.
        consumer_ref = ModelRef(name="model-a", controller="foo")
        provider_ref = ModelRef(name="model-b", controller="foo")
        domain = _make_domain(
            {
                consumer_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="backend"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="serve", model=provider_ref
                            ),
                            offer_name="my-custom-workload-offer",
                        )
                    ],
                ),
                provider_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, consumer_ref)
        provider_id = add_charm_to_domain(provider, domain, provider_ref)
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)

        assert solver.check() == z3.sat
        model = solver.model()

        # THEN both integrations resolve to the user's own custom offer name, not a synthesized
        # one: the discovered mesh companion must ride the same Juju offer as the real,
        # user-declared CMR it describes.
        mesh_offer = domain.integration_offer_name(mesh_integration, model)
        workload_offer = domain.integration_offer_name(workload_integration, model)
        assert workload_offer == "my-custom-workload-offer"
        assert mesh_offer == "my-custom-workload-offer"

    def test_discovered_mesh_companion_reuses_explicit_user_cmr_url(self) -> None:
        # GIVEN the same setup as above, but the user's explicit CMR also declares an external
        # URL (e.g. a cross-controller offer whose URL can't be re-derived from controller info
        # alone).
        consumer_ref = ModelRef(name="model-a", controller="foo")
        provider_ref = ModelRef(name="model-b", controller="foo")
        domain = _make_domain(
            {
                consumer_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="backend"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="serve", model=provider_ref
                            ),
                            offer_name="my-custom-workload-offer",
                            url="foo:admin/model-b.my-custom-workload-offer",
                        )
                    ],
                ),
                provider_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, consumer_ref)
        provider_id = add_charm_to_domain(provider, domain, provider_ref)
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)

        assert solver.check() == z3.sat
        model = solver.model()

        # THEN the discovered mesh companion reuses the user's explicit URL too, not just the
        # offer name -- otherwise it would get re-synthesized from prov_mc and could point at a
        # different (or no) URL than the real, user-declared CMR.
        assert domain.integration_offer_url(mesh_integration, model) == "foo:admin/model-b.my-custom-workload-offer"

    def test_conflicting_user_declared_offer_names_for_same_pair_raise(self) -> None:
        # GIVEN two distinct, explicit user CMRs between the same charm pair (different
        # endpoints) that declare two DIFFERENT offer names. Since this codebase merges every
        # cross-model integration between the same charm pair onto a single Juju offer, these
        # two user-declared names cannot both be honored.
        consumer = _make_charm(
            "consumer-app",
            {
                "backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload"),
                "backend-alt": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload_alt"),
            },
        )
        provider = _make_charm(
            "provider-app",
            {
                "serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload"),
                "serve-alt": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload_alt"),
            },
        )
        consumer_ref = ModelRef(name="model-a")
        provider_ref = ModelRef(name="model-b")
        domain = _make_domain(
            {
                consumer_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="backend"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="serve", model=provider_ref
                            ),
                            offer_name="offer-one",
                        ),
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="backend-alt"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="serve-alt", model=provider_ref
                            ),
                            offer_name="offer-two",
                        ),
                    ],
                ),
                provider_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer_id = add_charm_to_domain(consumer, domain, consumer_ref)
        provider_id = add_charm_to_domain(provider, domain, provider_ref)
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]
        [workload_alt_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "workload_alt"
        ]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(workload_integration.exists)
        solver.add(workload_alt_integration.exists)
        assert solver.check() == z3.sat
        model = solver.model()

        # THEN resolving the offer name fails loudly instead of silently picking an arbitrary one
        with pytest.raises(ValueError, match="Conflicting user-declared"):
            domain.integration_offer_name(workload_integration, model)

    def test_explicitly_declared_mesh_cmr_with_conflicting_offer_name_raises_on_extraction(self) -> None:
        # GIVEN a spec that explicitly declares BOTH the real workload CMR AND the
        # cross_model_mesh companion CMR between the same charm pair, each under its own
        # (different) user-supplied offer name. This is the extraction-time counterpart to
        # test_conflicting_user_declared_offer_names_for_same_pair_raise: since the real relation
        # and the mesh relation are both fully user-declared here (not solver-discovered), the
        # per-model extraction loop resolves their offer names directly from
        # application_integrations rather than via the solver-discovered-companion path -- so the
        # conflict must be caught there too, not just for discovered companions.
        consumer_ref = ModelRef(name="model-a", controller="foo")
        provider_ref = ModelRef(name="model-b", controller="foo")
        domain = _make_domain(
            {
                consumer_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="backend"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="serve", model=provider_ref
                            ),
                            offer_name="offer-one",
                        ),
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="consumer", endpoint="require-cmr-mesh"),
                            endpoint_2=DomainApplicationEndpoint(
                                application="provider", endpoint="provide-cmr-mesh", model=provider_ref
                            ),
                            offer_name="offer-two",
                        ),
                    ],
                ),
                provider_ref: DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, consumer_ref)
        provider_id = add_charm_to_domain(provider, domain, provider_ref)
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)
        assert solver.check() == z3.sat
        model = solver.model()

        # THEN extracting the solution fails loudly instead of silently emitting the mesh
        # relation and the real relation under two different (conflicting) offers
        with pytest.raises(ValueError, match="Conflicting user-declared"):
            extract_solution(model, domain, logging.getLogger("test"))

    def test_two_unrelated_cross_model_endpoints_still_share_one_offer(self) -> None:
        # GIVEN two charms in different models, related on TWO distinct, ordinary (non-mesh)
        # endpoints. This demonstrates that offer coupling in domain.py has no cross_model_mesh
        # -specific knowledge: it groups by charm pair alone.
        consumer = _make_charm(
            "consumer-app",
            {
                "backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload"),
                "backend-alt": CharmEndpoint(type=EndpointType.REQUIRES, interface="workload_alt"),
            },
        )
        provider = _make_charm(
            "provider-app",
            {
                "serve": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload"),
                "serve-alt": CharmEndpoint(type=EndpointType.PROVIDES, interface="workload_alt"),
            },
        )
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, provider_id)

        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]
        [workload_alt_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "workload_alt"
        ]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(workload_integration.exists)
        solver.add(workload_alt_integration.exists)
        assert solver.check() == z3.sat
        model = solver.model()

        assert domain.integration_offer_name(workload_integration, model) == domain.integration_offer_name(
            workload_alt_integration, model
        )

    def test_bidirectional_cross_model_pair_keeps_separate_offers_per_direction(self) -> None:
        # GIVEN two charms in different models that each provide a distinct endpoint to the
        # other (a mutually-required pair, e.g. dex-auth/oidc-gatekeeper's dex-oidc-config <->
        # oidc-client cyclic relation). Offer-sharing must NOT merge these into one offer: a
        # Juju offer is hosted by a single application, so charm-a's offer (exposing endpoint-a)
        # and charm-b's offer (exposing endpoint-b) are necessarily two separate offers.
        charm_a = _make_charm(
            "charm-a",
            {
                "provide-a": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface-a", cyclic=True),
                "require-b": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface-b"),
            },
        )
        charm_b = _make_charm(
            "charm-b",
            {
                "require-a": CharmEndpoint(type=EndpointType.REQUIRES, interface="iface-a", cyclic=True),
                "provide-b": CharmEndpoint(type=EndpointType.PROVIDES, interface="iface-b"),
            },
        )
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"a": DomainApplication(charm="charm-a")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"b": DomainApplication(charm="charm-b")},
                ),
            }
        )
        charm_a_id = add_charm_to_domain(charm_a, domain, ModelRef(name="model-a"))
        charm_b_id = add_charm_to_domain(charm_b, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, charm_a_id, charm_b_id)

        [iface_a_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "iface-a"]
        [iface_b_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "iface-b"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(iface_a_integration.exists)
        solver.add(iface_b_integration.exists)
        assert solver.check() == z3.sat
        model = solver.model()

        # THEN each direction keeps its own offer name (charm-a hosts one offer, charm-b hosts
        # the other), rather than colliding on a single shared name.
        offer_a = domain.integration_offer_name(iface_a_integration, model)
        offer_b = domain.integration_offer_name(iface_b_integration, model)
        assert offer_a != offer_b


class TestCrossModelMeshCompanionOverrideConstraint:
    """The companion requirement is no longer a core-solver rule (see issue #980's resolution):
    it is expressed per-charm as an override constraint using the general-purpose DSL, e.g.
    ``charms(cross_model(endpoint[x])) == charms(cross_model(endpoint[provide-cmr-mesh]))``.
    These tests exercise that DSL expression directly to confirm it has the intended semantics.

    The equality alone is vacuous for a purely local (same-model) mesh relation: cross_model()
    ignores local integrations, so both sides would read as the empty set regardless of whether
    provide-cmr-mesh is actually integrated. Since cross_model_mesh only makes sense to describe
    a genuine CMR, every override also asserts that provide-cmr-mesh, if integrated at all, is
    entirely cross-model -- ``len(endpoint[provide-cmr-mesh]) ==
    len(cross_model(endpoint[provide-cmr-mesh]))`` -- which closes that gap.
    """

    def _companion_constraint_expr(self, provider_id: int, domain: Domain) -> z3.BoolRef:
        ctx = LoweringContext(charm_id=provider_id, domain_charm=domain.charms[provider_id], domain=domain)
        expr = parse_constraint(
            "charms(cross_model(endpoint[serve])) == charms(cross_model(endpoint[provide-cmr-mesh]))"
            " and len(endpoint[provide-cmr-mesh]) == len(cross_model(endpoint[provide-cmr-mesh]))"
        )
        return lower(expr, ctx).expr

    def test_local_only_cmr_mesh_pairing_is_rejected_by_the_companion_constraint(self) -> None:
        # GIVEN two charms in the SAME model, related via BOTH cross_model_mesh and workload.
        # A local-only mesh relation carries no CMR data at all (there is no cross-model
        # relation for it to describe), so cross_model_mesh should never be used purely
        # in-model: the companion constraint's cross-model-only clause rejects it even though
        # the bare equality alone would hold vacuously (both sides empty).
        domain = _make_domain(
            {
                ModelRef(name="default"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "consumer": DomainApplication(charm="consumer-app"),
                        "provider": DomainApplication(charm="provider-app"),
                    },
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="default"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="default"))
        pair_charms_in_domain(domain, consumer_id, provider_id)
        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)
        solver.add(self._companion_constraint_expr(provider_id, domain))

        assert solver.check() == z3.unsat

    def test_cross_model_cmr_mesh_pairing_without_companion_is_rejected(self) -> None:
        # GIVEN two charms in different models, related ONLY via cross_model_mesh (no workload)
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, provider_id)
        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(z3.Not(workload_integration.exists))
        solver.add(self._companion_constraint_expr(provider_id, domain))

        # THEN unsatisfiable: charms(cross_model(provide-cmr-mesh)) is non-empty (the mesh
        # integration is cross-model and exists) but charms(cross_model(serve)) is empty, so the
        # two sets cannot be equal
        assert solver.check() == z3.unsat

    def test_cross_model_cmr_mesh_pairing_with_companion_is_satisfiable(self) -> None:
        # GIVEN two charms in different models, related via BOTH cross_model_mesh and workload
        domain = _make_domain(
            {
                ModelRef(name="model-a"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"consumer": DomainApplication(charm="consumer-app")},
                ),
                ModelRef(name="model-b"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={"provider": DomainApplication(charm="provider-app")},
                ),
            }
        )
        consumer, provider = _mesh_pair_charms()
        consumer_id = add_charm_to_domain(consumer, domain, ModelRef(name="model-a"))
        provider_id = add_charm_to_domain(provider, domain, ModelRef(name="model-b"))
        pair_charms_in_domain(domain, consumer_id, provider_id)
        [mesh_integration] = [
            i for i in domain.charm_integrations if domain.integration_interface(i) == "cross_model_mesh"
        ]
        [workload_integration] = [i for i in domain.charm_integrations if domain.integration_interface(i) == "workload"]

        solver = z3.Solver()
        add_constraints(solver, domain)
        solver.add(mesh_integration.exists)
        solver.add(workload_integration.exists)
        solver.add(self._companion_constraint_expr(provider_id, domain))

        # THEN satisfiable: both sets contain exactly {provider-app}
        assert solver.check() == z3.sat
