# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logic tests for optional config keys referenced by DSL constraints.

An override may declare a config key as *optional* by including ``null`` among
its allowed values::

    configs:
      role: [replication, config-server, shard, null]

Such a key gets both a value variable and an ``is_set`` variable.  A constraint
that reads ``config[role]`` must never be satisfiable by a value the solver
declines to actually set, because unset keys are omitted from the generated
bundle and the charm then falls back to its Charmhub default.

Regression coverage for the mongodb-k8s sharding failure, where a bundle was
emitted relating ``sharding`` <-> ``config-server`` with no ``role`` set on
either application, so both deployed as replica sets and blocked with
"The sharding interface cannot be used by replica sets."
"""

from collections.abc import Mapping
from typing import TypeAlias

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import Charm, CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm

ConfigValues: TypeAlias = dict[str, list[str | int | float | bool | None]]

# Mirrors static/charm-overrides/mongodb-k8s.yaml.
MONGODB_ROLE_CONSTRAINTS = [
    'bool(endpoint[config-server]) == (config[role] == "config-server")',
    'bool(endpoint[sharding]) == (config[role] == "shard")',
]


def make_mongodb(name: str = "mongodb-k8s", *, default_role: str | None = "replication") -> Charm:
    """Build a mongodb-k8s-like charm with an optional role config."""
    return make_charm(
        name,
        endpoints={
            "sharding": CharmEndpoint(type=EndpointType.REQUIRES, interface="shards", optional=True),
            "config-server": CharmEndpoint(type=EndpointType.PROVIDES, interface="shards", optional=True),
        },
        configs={"role": ["replication", "config-server", "shard", None]},
        config_defaults={"role": default_role} if default_role is not None else {},
        constraint_strs=MONGODB_ROLE_CONSTRAINTS,
    )


class TestOptionalConfigInConstraints:
    """An optional config read by a constraint must be emitted when it matters."""

    def test_sharding_integration_emits_matching_role_config(self) -> None:
        # GIVEN two mongodb-k8s applications whose override ties the role config
        # to the sharding/config-server endpoints
        mongodb = make_mongodb()
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN building a bundle that relates sharding <-> config-server
        bundle = build_single_model(
            builder,
            applications={
                "neighbor": AppSpec(charm="mongodb-k8s"),
                "target": AppSpec(charm="mongodb-k8s"),
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

        # THEN both applications must carry an explicit role matching their endpoint,
        # rather than silently falling back to the replication default
        assert bundle.applications["neighbor"].config.get("role") == "shard"
        assert bundle.applications["target"].config.get("role") == "config-server"

    def test_unset_optional_config_cannot_satisfy_a_value_constraint(self) -> None:
        # GIVEN a charm whose role config has no Charmhub default at all, so an
        # unset role can never legitimately equal "shard" or "config-server"
        mongodb = make_mongodb(default_role=None)
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN building a bundle that relates sharding <-> config-server
        bundle = build_single_model(
            builder,
            applications={
                "neighbor": AppSpec(charm="mongodb-k8s"),
                "target": AppSpec(charm="mongodb-k8s"),
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

        # THEN the role is still emitted explicitly for both applications
        assert bundle.applications["neighbor"].config.get("role") == "shard"
        assert bundle.applications["target"].config.get("role") == "config-server"

    def test_standalone_deployment_leaves_optional_config_unset(self) -> None:
        # GIVEN a single mongodb-k8s application with no sharding integration
        mongodb = make_mongodb()
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN building a bundle with no sharding relation
        bundle = build_single_model(builder, applications={"solo": AppSpec(charm="mongodb-k8s")})

        # THEN the role stays unset: pinning unset configs to the default must not
        # force every optional key to be written out
        assert "role" not in bundle.applications["solo"].config

    def test_role_pinned_to_conflicting_value_is_unsatisfiable(self) -> None:
        # GIVEN a charm whose role is fixed to "replication" by the override
        mongodb = make_charm(
            "mongodb-k8s",
            endpoints={
                "sharding": CharmEndpoint(type=EndpointType.REQUIRES, interface="shards", optional=True),
                "config-server": CharmEndpoint(type=EndpointType.PROVIDES, interface="shards", optional=True),
            },
            configs={"role": ["replication"]},
            config_defaults={"role": "replication"},
            constraint_strs=MONGODB_ROLE_CONSTRAINTS,
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN a sharding integration is requested anyway
        # THEN the bundle is rejected rather than emitted in an invalid topology
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "neighbor": AppSpec(charm="mongodb-k8s"),
                    "target": AppSpec(charm="mongodb-k8s"),
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


class TestComparisonsAgainstAbsentValues:
    """A key with no default has no value when unset, so constraints reading it must be false."""

    def test_membership_test_cannot_be_satisfied_by_an_unset_key(self) -> None:
        # GIVEN a constraint that reads the value via `in` rather than a comparison.
        # The guard is applied when lowering the whole constraint, so every operator
        # is covered -- not just the comparison operators.
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['config[role] in {"shard"}'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key is emitted rather than satisfied while absent
        assert bundle.applications["app"].config["role"] == "shard"

    def test_membership_test_against_values_outside_allowed_list_is_rejected(self) -> None:
        # GIVEN a membership test naming only values the key is not allowed to take
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['config[role] in {"bogus", "zzz"}'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        # THEN it is rejected rather than quietly satisfied with the key omitted
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, applications={"app": AppSpec(charm="probe")})

    def test_set_config_predicate_is_not_guarded(self) -> None:
        # GIVEN a constraint that explicitly asserts the key is NOT set.  `set(config[k])`
        # asks whether the key is set, so guarding it on is_set would make this
        # unsatisfiable.
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=["not set(config[role])"],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key is legitimately left unset
        assert "role" not in bundle.applications["app"].config

    def test_comparison_to_value_outside_allowed_list_is_rejected(self) -> None:
        # GIVEN a charm whose role has no Charmhub default, and a constraint comparing
        # it to a value that is not among the declared allowed values
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['config[role] == "bogus"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        # THEN the bundle is rejected.  Previously the solver satisfied this by picking
        # "bogus" for an unset key, then omitted the key entirely, so the deployed charm
        # never matched what the solver reasoned about.
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, applications={"app": AppSpec(charm="probe")})

    def test_inequality_against_absent_value_forces_the_key_to_be_set(self) -> None:
        # GIVEN a config with no Charmhub default and a `!=` constraint against it
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['config[role] != "shard"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key is set explicitly rather than left absent.  Treating a comparison
        # against an absent value as false is conservative: it errs towards emitting a
        # concrete value instead of letting the solver reason about a value the charm
        # will never have.
        assert bundle.applications["app"].config["role"] == "replication"

    def test_inequality_uses_the_default_when_one_exists(self) -> None:
        # GIVEN the same constraint on a config that does have a Charmhub default
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={"role": "replication"},
            constraint_strs=['config[role] != "shard"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key stays unset: the default already satisfies the constraint, and the
        # solver correctly reasons about the value the charm will actually use.
        assert "role" not in bundle.applications["app"].config

    def test_unreferenced_no_default_key_is_not_forced_to_be_set(self) -> None:
        # GIVEN a boolean config with no default that no constraint refers to
        charm = make_charm(
            "probe",
            configs={"debug": [True, False, None]},
            config_defaults={},
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key stays unset.  Forbidding every allowed value when unset would make
        # a two-valued boolean unsatisfiable and force it to be emitted needlessly.
        assert "debug" not in bundle.applications["app"].config


class TestOptionalResourcesInConstraints:
    """Resources share the flaw: an unset optional resource is omitted from the bundle.

    Resources carry no Charmhub default -- `default` is populated only for `fixed_value`
    resources, which have no Z3 variable -- so the absent-value guard is the whole of
    their handling.  These cases all built with `resources: {}` before the fix.
    """

    def test_resource_constraint_forces_the_resource_to_be_emitted(self) -> None:
        # GIVEN an optional resource pinned by a constraint to a non-default value
        charm = make_charm(
            "probe",
            resources={"my-image": ["default-image", "custom", None]},
            constraint_strs=['resource[my-image] == "custom"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the resource is actually written out rather than satisfied while absent
        assert bundle.applications["app"].resources["my-image"] == "custom"

    def test_resource_membership_test_cannot_be_satisfied_by_an_unset_resource(self) -> None:
        # GIVEN the same requirement expressed with `in` rather than `==`
        charm = make_charm(
            "probe",
            resources={"my-image": ["default-image", "custom", None]},
            constraint_strs=['resource[my-image] in {"custom"}'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the guard applies to every operator, not just comparisons
        assert bundle.applications["app"].resources["my-image"] == "custom"

    def test_resource_constrained_to_a_value_outside_allowed_list_is_rejected(self) -> None:
        # GIVEN a constraint naming a value the resource is not allowed to take
        charm = make_charm(
            "probe",
            resources={"my-image": ["default-image", "custom", None]},
            constraint_strs=['resource[my-image] == "bogus"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        # THEN it is rejected rather than quietly satisfied with the resource omitted
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, applications={"app": AppSpec(charm="probe")})

    def test_unreferenced_optional_resource_is_not_forced_to_be_set(self) -> None:
        # GIVEN an optional resource that no constraint refers to
        charm = make_charm("probe", resources={"my-image": ["default-image", "custom", None]})
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN it stays unset: the guard must not force resources to be emitted needlessly
        assert "my-image" not in bundle.applications["app"].resources


class TestAbsentValueGuardPreservesBooleanComposition:
    """The absent-value guard must not leak outside its own atomic predicate.

    A guard scoped to the whole constraint would force a no-default key to be set
    even in an inactive Boolean branch -- e.g. a false implication antecedent, or an
    explicit `not set(...)` escape hatch -- which is not what those constraints ask
    for.  The guard must instead be local to whichever comparison/`in`/direct boolean
    read actually consumes the value, letting And/Or/Not/Implies compose normally.
    """

    def test_implication_with_false_antecedent_leaves_the_key_unforced(self) -> None:
        # GIVEN a no-default key referenced only in the consequent of an implication
        # whose antecedent is false (the endpoint is optional and never integrated)
        charm = make_charm(
            "probe",
            endpoints={"x": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", optional=True)},
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['bool(endpoint[x]) => config[role] == "shard"'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key is not forced to be set: the implication is vacuously true, so
        # nothing actually reads the value
        assert "role" not in bundle.applications["app"].config

    def test_explicit_unset_escape_hatch_via_or_is_still_satisfiable(self) -> None:
        # GIVEN a constraint that explicitly permits the key to stay unset via `or`
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=[
                'not set(config[role]) or config[role] == "shard"',
                # A second constraint that only holds when role is genuinely unset,
                # to prove the escape hatch above is satisfiable rather than merely
                # unchosen by the optimizer.
                "not set(config[role])",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN both constraints hold together: the key stays unset
        assert "role" not in bundle.applications["app"].config

    def test_direct_boolean_read_of_a_bare_key_is_still_guarded(self) -> None:
        # GIVEN a boolean, no-default config used directly (not via comparison) as a
        # constraint, negated
        charm = make_charm(
            "probe",
            configs={"debug": [True, False, None]},
            config_defaults={},
            constraint_strs=["not config[debug]"],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the negation is pushed inside the guard, so it means the same as
        # `config[debug] == False` rather than being satisfiable by omitting the key
        assert bundle.applications["app"].config["debug"] is False

    def test_bare_key_read_as_a_boolean_cannot_be_satisfied_while_unset(self) -> None:
        # GIVEN a bare boolean read that is required to hold, alongside a constraint
        # demanding the key stay unset
        charm = make_charm(
            "probe",
            configs={"debug": [True, False, None]},
            config_defaults={},
            constraint_strs=["config[debug]", "not set(config[debug])"],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        # THEN the pair is contradictory rather than yielding a bundle whose `debug`
        # is true only inside the solver: the top-level read is guarded too
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, applications={"app": AppSpec(charm="probe")})

    def test_membership_test_still_reachable_inside_an_or(self) -> None:
        # GIVEN the 'in' path (the operator missed by the earlier, narrower fix)
        # combined with an explicit unset escape hatch
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=[
                'not set(config[role]) or config[role] in {"shard"}',
                "not set(config[role])",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the key still stays unset: the guard on 'in' is scoped to its own
        # operand, not the surrounding 'or'
        assert "role" not in bundle.applications["app"].config


class TestNegationIsSymmetricWithTheNegatedOperator:
    """`not (a == b)` and `a != b` must build the same bundle.

    Negating a guarded atom from the outside would negate its guard away --
    `Not(And(is_set, role == "shard"))` is satisfied by leaving the key unset --
    so the parenthesised spelling would quietly permit an omitted key while the
    direct operator spelling required one.
    """

    @staticmethod
    def _build(constraint: str) -> Mapping[str, object]:
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=[constraint],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})
        return bundle.applications["app"].config

    def test_not_equals_and_negated_equals_agree(self) -> None:
        # GIVEN the same requirement written both ways
        # WHEN building each
        direct = self._build('config[role] != "shard"')
        negated = self._build('not (config[role] == "shard")')

        # THEN both force a value to be emitted, and it is not the excluded one
        assert direct["role"] != "shard"
        assert negated["role"] != "shard"
        assert negated.keys() == direct.keys()

    def test_not_in_and_negated_in_agree(self) -> None:
        # GIVEN the membership form of the same requirement, written both ways
        # WHEN building each
        direct = self._build('config[role] not in {"shard"}')
        negated = self._build('not (config[role] in {"shard"})')

        # THEN both likewise pin the key rather than omitting it
        assert direct["role"] != "shard"
        assert negated["role"] != "shard"
        assert negated.keys() == direct.keys()

    def test_negation_does_not_force_keys_it_never_reads(self) -> None:
        # GIVEN a negated comparison sitting in an inactive Boolean branch
        charm = make_charm(
            "probe",
            endpoints={"x": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", optional=True)},
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['bool(endpoint[x]) => not (config[role] == "shard")'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN pushing the negation inward keeps the guard atomic: nothing reads the
        # value, so the key stays unset
        assert "role" not in bundle.applications["app"].config


class TestNegationNestingPreservesGuards:
    """Guards must survive `not` at any depth, not just one immediate atom.

    An outer negation cancels an atom's guard, so a nested `not` would undo the
    guard the inner one installed.  `not not config[debug]` built `config: {}`
    while the equivalent `config[debug]` emitted `debug: True`, and
    `not (config[role] == "shard" or ...)` was satisfiable with the key omitted --
    both reintroducing the phantom value this module rules out.  Negation is
    normalised down to the atoms (double negation, De Morgan) so that cannot happen.
    """

    @staticmethod
    def _build(constraint: str, configs: ConfigValues) -> Mapping[str, object]:
        charm = make_charm("probe", configs=configs, config_defaults={}, constraint_strs=[constraint])
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})
        return bundle.applications["app"].config

    def test_double_negation_means_the_same_as_no_negation(self) -> None:
        # GIVEN a bare boolean read wrapped in two negations
        configs: ConfigValues = {"debug": [True, False, None]}

        # WHEN building it and the un-negated equivalent
        doubled = self._build("not not config[debug]", configs)
        plain = self._build("config[debug]", configs)

        # THEN the inner guard survives the outer negation instead of being cancelled
        assert doubled["debug"] is True
        assert doubled.keys() == plain.keys()

    def test_triple_negation_means_the_same_as_one_negation(self) -> None:
        # GIVEN an odd number of negations, so the normalisation must not simply
        # cancel every pair and lose the sign
        configs: ConfigValues = {"debug": [True, False, None]}

        # WHEN building
        tripled = self._build("not not not config[debug]", configs)

        # THEN it agrees with the single-negation form, which pins the key to False
        assert tripled["debug"] is False

    def test_negated_disjunction_cannot_be_satisfied_by_omitting_the_key(self) -> None:
        # GIVEN a negation wrapping a disjunction that between them exclude every
        # value the key is allowed to take
        charm = make_charm(
            "probe",
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['not (config[role] == "shard" or config[role] == "replication")'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        # THEN De Morgan pushes the negation through, so this is contradictory rather
        # than satisfiable by leaving the key unset
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, applications={"app": AppSpec(charm="probe")})

    def test_negated_conjunction_forces_the_key_when_the_other_side_holds(self) -> None:
        # GIVEN a negated conjunction whose other conjunct is unconditionally true
        configs: ConfigValues = {"role": ["replication", "shard", None]}

        # WHEN building
        config = self._build('not (config[role] == "shard" and config[role] != "replication")', configs)

        # THEN the surviving disjunct pins the key rather than omitting it
        assert config["role"] == "replication"

    def test_negated_conjunction_leaves_the_key_alone_when_the_other_side_fails(self) -> None:
        # GIVEN a negated conjunction whose other conjunct is false (the optional
        # endpoint is never integrated), so nothing needs to read the value
        charm = make_charm(
            "probe",
            endpoints={"x": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", optional=True)},
            configs={"role": ["replication", "shard", None]},
            config_defaults={},
            constraint_strs=['not (config[role] == "shard" and bool(endpoint[x]))'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN normalising the negation must not over-constrain: the key stays unset
        assert "role" not in bundle.applications["app"].config

    def test_double_negated_set_predicate_still_asks_only_about_presence(self) -> None:
        # GIVEN `set(...)` under two negations -- it must reach the plain-negation
        # fallback both times rather than being absorbed as a value read
        configs: ConfigValues = {"role": ["replication", "shard", None]}

        # WHEN building
        config = self._build("not not set(config[role])", configs)

        # THEN it means `set(config[role])`: some value is written, unconstrained
        assert "role" in config

    def test_negated_negative_membership_test_is_absorbed(self) -> None:
        # GIVEN a negation wrapping an already-negative membership test
        configs: ConfigValues = {"role": ["replication", "shard", None]}

        # WHEN building
        config = self._build('not (config[role] not in {"shard"})', configs)

        # THEN the two cancel to a positive membership test, guard intact
        assert config["role"] == "shard"

    def test_negated_implication_keeps_the_guard_in_the_consequent(self) -> None:
        # GIVEN a negated implication whose antecedent holds independently of the
        # guarded key, so only the consequent's own guard can force it to be set
        charm = make_charm(
            "probe",
            configs={"mode": ["on", "off"], "role": ["replication", "shard", None]},
            config_defaults={"mode": "on"},
            constraint_strs=['not (config[mode] == "on" => config[role] == "shard")'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm))

        # WHEN building
        bundle = build_single_model(builder, applications={"app": AppSpec(charm="probe")})

        # THEN the consequent is negated as an atom, keeping its guard: the key is
        # written out rather than the negation being satisfied by omitting it
        assert bundle.applications["app"].config["role"] == "replication"
