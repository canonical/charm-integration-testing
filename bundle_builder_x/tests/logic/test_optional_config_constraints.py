# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Regression tests for #919: an unset optional config/resource must not satisfy a
DSL constraint, since unset keys are omitted from the emitted bundle.
"""

from bundle_builder_x.bundle import Bundle
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


def _make_mongo(name: str) -> Charm:
    return make_charm(
        name,
        endpoints={
            "sharding": CharmEndpoint(type=EndpointType.PROVIDES, interface="shards", optional=True),
            "config-server": CharmEndpoint(type=EndpointType.REQUIRES, interface="shards", optional=True),
        },
        configs={"role": ["replication", "config-server", "shard", None]},
        constraint_strs=[
            'bool(endpoint[config-server]) == (config[role] == "config-server")',
            'bool(endpoint[sharding]) == (config[role] == "shard")',
        ],
    )


class TestOptionalConfigConstraints:
    def test_sharding_integration_emits_matching_role_config(self) -> None:
        # GIVEN two mongo charms related over sharding <-> config-server
        neighbor = _make_mongo("mongo-neighbor")
        target = _make_mongo("mongo-target")
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(neighbor, target))

        # WHEN a bundle is built relating them
        bundle = build_single_model(
            builder,
            applications={
                "neighbor": AppSpec(charm="mongo-neighbor"),
                "target": AppSpec(charm="mongo-target"),
            },
            integrations=[
                IntegrationSpec(
                    application="neighbor",
                    endpoint="sharding",
                    remote_application="target",
                    remote_endpoint="config-server",
                ),
            ],
        )

        # THEN both applications carry a `role` config matching their endpoint,
        # rather than omitting it and silently violating the constraint.
        neighbor_app = bundle.applications["neighbor"]
        target_app = bundle.applications["target"]
        assert neighbor_app.config.get("role") == "shard"
        assert target_app.config.get("role") == "config-server"

    def test_standalone_deployment_leaves_optional_config_unset(self) -> None:
        # GIVEN a single mongo charm with no integrations at all
        solo = _make_mongo("mongo-solo")
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(solo))

        # WHEN a bundle is built with just that one application
        bundle = build_single_model(
            builder,
            applications={"solo": AppSpec(charm="mongo-solo")},
        )

        # THEN the fix does not over-constrain: an irrelevant optional config
        # key is still left unset rather than forced to a value.
        assert "role" not in bundle.applications["solo"].config


def _make_flagged(name: str, constraint: str) -> Charm:
    return make_charm(
        name,
        endpoints={
            "trigger": CharmEndpoint(type=EndpointType.REQUIRES, interface="trigger-if", optional=True),
        },
        configs={"dev": [True, False, None]},
        constraint_strs=[constraint],
    )


def _make_trigger_provider(name: str) -> Charm:
    return make_charm(
        name,
        endpoints={
            "trigger": CharmEndpoint(type=EndpointType.PROVIDES, interface="trigger-if", optional=True),
        },
    )


def _build_flagged_and_provider(flagged: Charm, provider: Charm) -> Bundle:
    builder = BundleBuilder(charmhub_client=CharmhubClientStub(flagged, provider))
    return build_single_model(
        builder,
        applications={flagged.name: AppSpec(charm=flagged.name), provider.name: AppSpec(charm=provider.name)},
        integrations=[
            IntegrationSpec(
                application=flagged.name,
                endpoint="trigger",
                remote_application=provider.name,
                remote_endpoint="trigger",
            ),
        ],
    )


class TestUnrepresentableBoolAbsentGuard:
    """A bool config with both true/false allowed and no default has no third
    value to pin `var` to when unset (see DomainCharmConfig.bool_as_int), so
    it's encoded as an Int (0/1) instead, letting `constraints.py` pin unset
    to a value outside {0, 1} the same way it already does for strings/ints.

    The DSL only accepts a bare Runtime-typed `config[key]` as a whole
    constraint or under `not`; `and`/`or`/`=>` require an explicit `not` to
    convert it to Bool first, so every case below is reachable DSL.
    """

    def test_unconditional_bare_read_forces_the_key_to_be_set(self) -> None:
        # GIVEN dev is used, bare, as the entire constraint (always true)
        app = make_charm(
            "flagged-top",
            configs={"dev": [True, False, None]},
            constraint_strs=["config[dev]"],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app))

        # WHEN a bundle is built
        bundle = build_single_model(builder, applications={"flagged-top": AppSpec(charm="flagged-top")})

        # THEN dev is emitted true rather than omitted
        assert bundle.applications["flagged-top"].config.get("dev") is True

    def test_negated_bare_bool_read_can_be_satisfied_by_leaving_the_key_unset(self) -> None:
        # GIVEN dev must be "not true" whenever the trigger endpoint is integrated
        app = _make_flagged("flagged-neg", "bool(endpoint[trigger]) => not config[dev]")
        bundle = _build_flagged_and_provider(app, _make_trigger_provider("provider-neg"))

        # THEN this does not force emission: an unset key is already "not true",
        # consistent with how `!=` behaves against an unset key of any other type.
        assert "dev" not in bundle.applications["flagged-neg"].config

    def test_double_negation_matches_the_positive_read(self) -> None:
        # GIVEN dev must be true (via double negation) whenever trigger is integrated
        app = _make_flagged("flagged-dn", "bool(endpoint[trigger]) => not not config[dev]")
        bundle = _build_flagged_and_provider(app, _make_trigger_provider("provider-dn"))

        # THEN dev is emitted true: an even number of `not`s must not collapse
        # back to leaving the key unset.
        assert bundle.applications["flagged-dn"].config.get("dev") is True

    def test_unreferenced_bool_config_stays_unset(self) -> None:
        # GIVEN a constraint that would force dev true if the trigger endpoint
        # were integrated, with no integration to trigger it
        app = _make_flagged("flagged-solo", "bool(endpoint[trigger]) => not not config[dev]")
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app))

        # WHEN a bundle is built with just that one application
        bundle = build_single_model(builder, applications={"flagged-solo": AppSpec(charm="flagged-solo")})

        # THEN the fix does not over-constrain: dev is still left unset
        assert "dev" not in bundle.applications["flagged-solo"].config
