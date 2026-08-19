# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logic tests for multi-controller deployments with same-name models.

These tests cover the spec-to-domain pipeline when two models share the same
name but live on different controllers.  The domain key for such models is
``controller/name``; the solver must correctly resolve CMRs across them.
"""

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec, ModelSpec, SpecFile

from .conftest import JUJU_VERSION, CharmhubClientStub, build_multi_model, make_charm

# ---------------------------------------------------------------------------
# Spec validation: valid cases
# ---------------------------------------------------------------------------


class TestSpecFileMultiControllerValid:
    """SpecFile validation accepts same-name models on different controllers."""

    @dataclass
    class Params:
        label: str
        models: list[ModelSpec]
        expected_keys: list[str]
        absent_keys: list[str]

    test_cases = [
        Params(
            label="same_name_different_controllers",
            models=[
                ModelSpec(name="prod", controller="lxd", applications={"app": AppSpec(charm="c")}),
                ModelSpec(name="prod", controller="k8s", applications={"app": AppSpec(charm="c")}),
            ],
            expected_keys=["lxd/prod", "k8s/prod"],
            absent_keys=["prod"],
        ),
        Params(
            label="unambiguous_name_has_plain_alias",
            models=[
                ModelSpec(name="prod", controller="lxd", applications={"app": AppSpec(charm="c")}),
            ],
            expected_keys=["lxd/prod", "prod"],
            absent_keys=[],
        ),
        Params(
            label="no_controller_plain_name_only",
            models=[
                ModelSpec(name="prod", applications={"app": AppSpec(charm="c")}),
            ],
            expected_keys=["prod"],
            absent_keys=["lxd/prod"],
        ),
        Params(
            label="mixed_controllers_unique_names",
            models=[
                ModelSpec(name="app-model", controller="lxd", applications={"a": AppSpec(charm="c")}),
                ModelSpec(name="db-model", controller="lxd", applications={"b": AppSpec(charm="c")}),
            ],
            expected_keys=["lxd/app-model", "lxd/db-model", "app-model", "db-model"],
            absent_keys=[],
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a spec built from the provided model list
        spec = SpecFile(models=params.models)
        # THEN the expected keys are all present in models_by_name
        for key in params.expected_keys:
            assert key in spec.models_by_name, f"expected key {key!r} to be in models_by_name"
        # AND the absent keys are not present
        for key in params.absent_keys:
            assert key not in spec.models_by_name, f"key {key!r} should not be in models_by_name"


# ---------------------------------------------------------------------------
# Spec validation: rejection cases
# ---------------------------------------------------------------------------


class TestSpecFileMultiControllerRejected:
    """SpecFile validation rejects duplicate models that cannot be disambiguated."""

    @dataclass
    class Params:
        label: str
        models: list[ModelSpec]
        error_match: str

    test_cases = [
        Params(
            label="same_name_same_controller",
            models=[
                ModelSpec(name="prod", controller="lxd", applications={"app": AppSpec(charm="c")}),
                ModelSpec(name="prod", controller="lxd", applications={"app": AppSpec(charm="c")}),
            ],
            error_match="Duplicate model name 'prod' on controller 'lxd'",
        ),
        Params(
            label="same_name_no_controller",
            models=[
                ModelSpec(name="prod", applications={"app": AppSpec(charm="c")}),
                ModelSpec(name="prod", applications={"app": AppSpec(charm="c")}),
            ],
            error_match="Duplicate model name",
        ),
        Params(
            label="ambiguous_cmr_plain_name_no_url",
            models=[
                ModelSpec(
                    name="prod",
                    controller="lxd",
                    applications={"lxd-app": AppSpec(charm="c")},
                    integrations=[
                        IntegrationSpec(
                            application="lxd-app",
                            endpoint="metrics",
                            remote_model="prod",  # ambiguous: not in models_by_name
                            remote_application="k8s-app",
                            remote_endpoint="scrape",
                        ),
                    ],
                ),
                ModelSpec(
                    name="prod",
                    controller="k8s",
                    applications={"k8s-app": AppSpec(charm="c")},
                ),
            ],
            error_match="requires a 'url' field",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a spec that should be rejected
        # THEN building it raises with the expected message
        with pytest.raises(ValueError, match=params.error_match):
            SpecFile(models=params.models)


# ---------------------------------------------------------------------------
# Spec validation: CMR with full controller/name key is accepted
# ---------------------------------------------------------------------------


class TestSpecFileCMRWithFullKey:
    """SpecFile accepts CMR specs that use remote_controller + remote_model for disambiguation."""

    @dataclass
    class Params:
        label: str
        models: list[ModelSpec]

    test_cases = [
        Params(
            label="cmr_uses_full_key_when_names_collide",
            models=[
                ModelSpec(
                    name="prod",
                    controller="lxd",
                    applications={"lxd-app": AppSpec(charm="c")},
                    integrations=[
                        IntegrationSpec(
                            application="lxd-app",
                            endpoint="metrics",
                            remote_model="prod",  # full key disambiguates
                            remote_controller="k8s",
                            remote_application="k8s-app",
                            remote_endpoint="scrape",
                            url="k8s:admin/prod.k8s-app-offer",
                        ),
                    ],
                ),
                ModelSpec(
                    name="prod",
                    controller="k8s",
                    applications={"k8s-app": AppSpec(charm="c")},
                ),
            ],
        ),
        Params(
            label="cmr_uses_plain_alias_when_unambiguous",
            models=[
                ModelSpec(
                    name="app-model",
                    controller="lxd",
                    applications={"my-app": AppSpec(charm="c")},
                    integrations=[
                        IntegrationSpec(
                            application="my-app",
                            endpoint="metrics",
                            remote_model="db-model",  # plain alias works when unambiguous
                            remote_controller="lxd",
                            remote_application="pg",
                            remote_endpoint="scrape",
                            url="lxd:admin/db-model.pg-offer",
                        ),
                    ],
                ),
                ModelSpec(
                    name="db-model",
                    controller="lxd",
                    applications={"pg": AppSpec(charm="c")},
                ),
            ],
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a spec with a CMR that uses the full key or a plain alias
        spec = SpecFile(models=params.models)
        # THEN the spec is valid
        assert len(spec.models) == 2


# ---------------------------------------------------------------------------
# Solver: CMR resolution and URL synthesis across same-name models
# ---------------------------------------------------------------------------


class TestMultiControllerSolver:
    """Solver and extraction tests for same-name models on different controllers."""

    @dataclass
    class Params:
        label: str
        prov_interface: str
        req_interface: str
        expected_url: str

    test_cases = [
        Params(
            label="postgresql_client_interface",
            prov_interface="postgresql_client",
            req_interface="postgresql_client",
            expected_url="lxd:admin/prod.postgresql-offer",
        ),
        Params(
            label="generic_pgsql_interface",
            prov_interface="pgsql",
            req_interface="pgsql",
            expected_url="lxd:admin/prod.postgresql-offer",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test_cmr_across_same_name_models(self, params: Params) -> None:
        # GIVEN a database provider on lxd/prod and a client on k8s/prod
        postgresql = make_charm(
            "postgresql",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface=params.prov_interface,
                    optional=True,
                ),
            },
        )
        webapp = make_charm(
            "webapp",
            endpoints={
                "db": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface=params.req_interface,
                    optional=False,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(postgresql, webapp))

        # WHEN building two models with the same name on different controllers
        # with an explicit CMR from k8s/prod (webapp) to lxd/prod (postgresql)
        solution = build_multi_model(
            builder,
            [
                ModelSpec(
                    name="prod",
                    controller="lxd",
                    platform="machine",
                    juju=JUJU_VERSION,
                    applications={"pg": AppSpec(charm="postgresql")},
                ),
                ModelSpec(
                    name="prod",
                    controller="k8s",
                    platform="kubernetes",
                    juju=JUJU_VERSION,
                    applications={"app": AppSpec(charm="webapp")},
                    integrations=[
                        IntegrationSpec(
                            application="app",
                            endpoint="db",
                            remote_model="prod",  # full key disambiguates
                            remote_controller="lxd",
                            remote_application="pg",
                            remote_endpoint="database",
                            offer_name="postgresql-offer",
                        ),
                    ],
                ),
            ],
        )

        # THEN both bundles are produced
        bundle_keys = {b.model for b in solution.bundles}
        assert "lxd/prod" in bundle_keys
        assert "k8s/prod" in bundle_keys

        # AND the k8s bundle contains the CMR pointing at lxd/prod
        k8s_bundle = next(b for b in solution.bundles if b.model == "k8s/prod")
        matching = [
            c
            for c in k8s_bundle.cross_model_integrations
            if c.remote_model == "lxd/prod" and c.remote_application == "pg"
        ]
        assert len(matching) == 1, "k8s bundle should contain exactly one CMR pointing at lxd/prod"

        # AND the URL uses the bare model name, not the domain key
        cmr = matching[0]
        assert cmr.url == params.expected_url
        assert "lxd/prod" not in (cmr.url or ""), "domain key must not appear in the URL path"
