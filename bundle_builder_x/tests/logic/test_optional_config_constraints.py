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

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm

# Mirrors static/charm-overrides/mongodb-k8s.yaml.
MONGODB_ROLE_CONSTRAINTS = [
    'bool(endpoint[config-server]) == (config[role] == "config-server")',
    'bool(endpoint[sharding]) == (config[role] == "shard")',
]


def make_mongodb(name: str = "mongodb-k8s", *, default_role: str | None = "replication"):
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
