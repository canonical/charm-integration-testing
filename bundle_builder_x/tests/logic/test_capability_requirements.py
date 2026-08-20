# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logic tests for capability requirements (feature-based endpoint selection).

Covers Section 5 of charm-deployment-constraints.rst:

When multiple charms provide the same interface, a requires charm may need to
distinguish between them based on declared endpoint features.  For example,
temporal-k8s needs ``temporal-admin-k8s`` specifically for its ``admin``
endpoint (not ``temporal-ui-k8s``, even though both provide the same interface).

The mechanism: a requires endpoint declares a set of features it must negotiate,
and a DSL constraint ensures the correct feature set is active when the endpoint
is integrated.

    bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import Charm, CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec, ModelSpec

from .conftest import JUJU_VERSION, CharmhubClientStub, build_multi_model, build_single_model, make_charm


class TestCapabilityRequirements:
    """Section 5: Feature-constrained endpoint selection."""

    def test_feature_constraint_selects_correct_provider(self) -> None:
        # GIVEN temporal-k8s with an admin endpoint that requires the "admin" feature
        # DSL constraint: when admin is connected, features must equal {"admin"}
        temporal = make_charm(
            "temporal-k8s",
            endpoints={
                "admin": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=False,
                    features=frozenset({"admin"}),
                ),
                "ui": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
            constraint_strs=[
                'bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}',
                'bool(endpoint[ui]) => features(endpoint[ui]) == {"ui"}',
            ],
        )
        # AND temporal-admin-k8s: provides temporal interface WITH the "admin" feature
        temporal_admin = make_charm(
            "temporal-admin-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"admin"}),
                ),
            },
        )
        # AND temporal-ui-k8s: provides temporal interface WITH the "ui" feature only
        temporal_ui = make_charm(
            "temporal-ui-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(temporal, temporal_admin, temporal_ui))

        # WHEN building with just temporal-k8s (admin endpoint is non-optional)
        bundle = build_single_model(
            builder,
            applications={"temporal": AppSpec(charm="temporal-k8s")},
        )

        # THEN temporal-admin-k8s is added (it has the "admin" feature)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert (
            "temporal-admin-k8s" in charm_names
        ), "temporal-admin-k8s should be selected to satisfy the admin feature requirement"

        # AND temporal-ui-k8s is NOT added for the admin endpoint
        # (it may be present if the solver also satisfies the optional ui endpoint,
        # but the admin integration must go to temporal-admin-k8s)
        admin_integration = next(
            (i for i in bundle.integrations if any(ep.endpoint == "admin" for ep in i)),
            None,
        )
        assert admin_integration is not None, "An admin integration should exist"
        admin_provider_app = next(ep.application for ep in admin_integration if ep.endpoint != "admin")
        assert (
            bundle.applications[admin_provider_app].charm.name == "temporal-admin-k8s"
        ), "The admin endpoint must integrate with temporal-admin-k8s, not temporal-ui-k8s"

    def test_wrong_provider_violates_feature_constraint(self) -> None:
        # GIVEN temporal-k8s requiring admin feature
        temporal = make_charm(
            "temporal-k8s",
            endpoints={
                "admin": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=False,
                    features=frozenset({"admin"}),
                ),
            },
            constraint_strs=[
                'bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}',
            ],
        )
        # AND ONLY temporal-ui-k8s available (has "ui" feature, not "admin")
        temporal_ui = make_charm(
            "temporal-ui-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(temporal, temporal_ui))

        # WHEN building - temporal-ui-k8s violates the feature constraint
        # THEN the solver cannot find a valid solution
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"temporal": AppSpec(charm="temporal-k8s")},
            )

    def test_feature_constraint_without_features_accepts_any_provider(self) -> None:
        # GIVEN a charm with a requires endpoint that declares NO feature requirement
        # (plain interface matching - any provider is acceptable)
        app = make_charm(
            "my-app",
            endpoints={
                "db": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgsql",
                    optional=False,
                )
            },
        )
        # AND two providers, neither with any feature declarations
        pg_a = make_charm(
            "postgresql-a",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        pg_b = make_charm(
            "postgresql-b",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app, pg_a, pg_b))

        # WHEN building - either provider is acceptable
        bundle = build_single_model(
            builder,
            applications={"app": AppSpec(charm="my-app")},
        )

        # THEN one of the two providers is selected (no feature constraint to discriminate)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        providers_added = charm_names & {"postgresql-a", "postgresql-b"}
        assert len(providers_added) >= 1


class TestFeatureCoherenceCrossModelConsistency:
    """SQT-1038 regression: feature coherence must be enforced the same way
    regardless of whether an integration is local or cross-model.

    Two charms that each unconditionally self-tag their own endpoint with a
    feature the *other* endpoint doesn't declare (e.g. katib-db-manager's
    "katib-service" tag vs. kfp-persistence's "kfp-api" tag) have mismatched,
    mutually-exclusive feature requirements. That mismatch is a real modeling
    conflict and must be rejected consistently in both topologies - it must
    not silently succeed only because the integration happens to be cross-model.
    """

    @staticmethod
    def _make_mismatched_charms() -> tuple[Charm, Charm]:
        # Provider self-tags its endpoint with "provider-tag", unconditionally required.
        provider = make_charm(
            "provider-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="generic",
                    optional=True,
                    features=frozenset({"provider-tag"}),
                ),
            },
            constraint_strs=['bool(endpoint[svc]) => "provider-tag" in features(endpoint[svc])'],
        )
        # Requirer self-tags its endpoint with a different, mutually exclusive tag.
        requirer = make_charm(
            "requirer-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="generic",
                    optional=False,
                    features=frozenset({"requirer-tag"}),
                ),
            },
            constraint_strs=['bool(endpoint[svc]) => "requirer-tag" in features(endpoint[svc])'],
        )
        return provider, requirer

    def test_mismatched_self_tags_fail_in_single_model(self) -> None:
        # GIVEN two charms whose unconditional self-tag assertions conflict
        provider, requirer = self._make_mismatched_charms()
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(provider, requirer))

        # WHEN building both applications into the same model
        # THEN the solver correctly rejects the mismatch
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "requirer": AppSpec(charm="requirer-app"),
                    "provider": AppSpec(charm="provider-app"),
                },
                integrations=[
                    IntegrationSpec(
                        application="requirer",
                        endpoint="svc",
                        remote_application="provider",
                        remote_endpoint="svc",
                    ),
                ],
            )

    def test_mismatched_self_tags_fail_across_models(self) -> None:
        # GIVEN the same mismatched charms, but integrated across two separate models (CMR)
        provider, requirer = self._make_mismatched_charms()
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(provider, requirer))

        # WHEN building with the integration expressed as a cross-model relation
        # THEN the solver must reject the mismatch just as it does in the single-model case -
        # this must NOT silently succeed just because the integration is cross-model.
        with pytest.raises(UncompletableBundleError):
            build_multi_model(
                builder,
                [
                    ModelSpec(
                        name="target",
                        controller="k8s",
                        platform="kubernetes",
                        juju=JUJU_VERSION,
                        applications={"requirer": AppSpec(charm="requirer-app")},
                        integrations=[
                            IntegrationSpec(
                                application="requirer",
                                endpoint="svc",
                                remote_model="neighbor",
                                remote_controller="k8s",
                                remote_application="provider",
                                remote_endpoint="svc",
                                offer_name="provider-offer",
                            ),
                        ],
                    ),
                    ModelSpec(
                        name="neighbor",
                        controller="k8s",
                        platform="kubernetes",
                        juju=JUJU_VERSION,
                        applications={"provider": AppSpec(charm="provider-app")},
                    ),
                ],
            )


class TestFeatureMismatchDiagnostics:
    """UncompletableBundleError.feature_mismatches must surface all three ways the
    feature-coherence check (SQT-1038) can fail, so the failure is diagnosable instead of
    falling back to the generic "Cannot expand domain..." message:

    1. requires-only: the requires endpoint declares a feature the provides endpoint doesn't.
    2. provides-only: the provides endpoint declares a feature the requires endpoint doesn't.
    3. value-conflict: both endpoints declare the *same* feature name, but other constraints
       force their booleans to disagree.
    """

    def test_requires_only_mismatch_is_reported(self) -> None:
        # GIVEN a requires endpoint that needs feature "admin"...
        temporal = make_charm(
            "temporal-k8s",
            endpoints={
                "admin": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=False,
                    features=frozenset({"admin"}),
                ),
            },
            constraint_strs=['bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}'],
        )
        # ...and the only available provider declares a different feature ("ui"), not "admin".
        temporal_ui = make_charm(
            "temporal-ui-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(temporal, temporal_ui))

        # WHEN building fails because of the mismatch
        with pytest.raises(UncompletableBundleError) as exc_info:
            build_single_model(
                builder,
                applications={"temporal": AppSpec(charm="temporal-k8s")},
            )

        # THEN the error reports the requires/provides endpoints and the missing feature
        mismatches = exc_info.value.feature_mismatches
        assert any(
            m.requires.charm_name == "temporal-k8s"
            and m.requires.endpoint == "admin"
            and m.provides.charm_name == "temporal-ui-k8s"
            and m.provides.endpoint == "temporal"
            and m.feature == "admin"
            for m in mismatches
        )

    def test_provides_only_mismatch_is_reported(self) -> None:
        # GIVEN a provider that unconditionally self-tags its endpoint with "provider-tag"...
        provider = make_charm(
            "provider-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="generic",
                    optional=True,
                    features=frozenset({"provider-tag"}),
                ),
            },
            constraint_strs=['bool(endpoint[svc]) => "provider-tag" in features(endpoint[svc])'],
        )
        # ...and the only requirer declares no feature requirement at all on that endpoint.
        requirer = make_charm(
            "requirer-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="generic",
                    optional=False,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(provider, requirer))

        # WHEN building fails because of the mismatch
        with pytest.raises(UncompletableBundleError) as exc_info:
            build_single_model(
                builder,
                applications={
                    "requirer": AppSpec(charm="requirer-app"),
                    "provider": AppSpec(charm="provider-app"),
                },
                integrations=[
                    IntegrationSpec(
                        application="requirer",
                        endpoint="svc",
                        remote_application="provider",
                        remote_endpoint="svc",
                    ),
                ],
            )

        # THEN the error reports the requires/provides endpoints and the unmatched feature
        mismatches = exc_info.value.feature_mismatches
        assert any(
            m.requires.charm_name == "requirer-app"
            and m.requires.endpoint == "svc"
            and m.provides.charm_name == "provider-app"
            and m.provides.endpoint == "svc"
            and m.feature == "provider-tag"
            for m in mismatches
        )

    def test_value_conflict_mismatch_is_reported(self) -> None:
        # GIVEN both endpoints declare the SAME feature name ("tag"), but their own
        # constraints force it to different truth values when the integration exists:
        # the provider requires it active, the requirer requires it inactive.
        provider = make_charm(
            "provider-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="generic",
                    optional=True,
                    features=frozenset({"tag"}),
                ),
            },
            constraint_strs=['bool(endpoint[svc]) => "tag" in features(endpoint[svc])'],
        )
        requirer = make_charm(
            "requirer-app",
            endpoints={
                "svc": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="generic",
                    optional=False,
                    features=frozenset({"tag"}),
                ),
            },
            constraint_strs=['bool(endpoint[svc]) => not ("tag" in features(endpoint[svc]))'],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(provider, requirer))

        # WHEN building fails because the two endpoints' shared feature is forced to conflict
        with pytest.raises(UncompletableBundleError) as exc_info:
            build_single_model(
                builder,
                applications={
                    "requirer": AppSpec(charm="requirer-app"),
                    "provider": AppSpec(charm="provider-app"),
                },
                integrations=[
                    IntegrationSpec(
                        application="requirer",
                        endpoint="svc",
                        remote_application="provider",
                        remote_endpoint="svc",
                    ),
                ],
            )

        # THEN the error reports the requires/provides endpoints and the conflicting feature
        mismatches = exc_info.value.feature_mismatches
        assert any(
            m.requires.charm_name == "requirer-app"
            and m.requires.endpoint == "svc"
            and m.provides.charm_name == "provider-app"
            and m.provides.endpoint == "svc"
            and m.feature == "tag"
            for m in mismatches
        )
