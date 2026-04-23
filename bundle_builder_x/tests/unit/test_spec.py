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

from pathlib import Path

import pytest
import yaml

from bundle_builder_x.domain_builder import (
    applications_from_spec,
    classify_integrations,
)
from bundle_builder_x.spec import (
    AppSpec,
    IntegrationSpec,
    ModelSpec,
    SpecFile,
)


class TestAppSpec:
    def test_minimal(self) -> None:
        # GIVEN a spec with only charm
        spec = AppSpec(charm="my-charm")
        # THEN defaults are None
        assert spec.charm == "my-charm"
        assert spec.channel is None
        assert spec.revision is None
        assert spec.base is None

    def test_full(self) -> None:
        # GIVEN a fully-specified app
        spec = AppSpec(charm="my-charm", channel="1/stable", revision=42, base="ubuntu@22.04")
        assert spec.channel == "1/stable"
        assert spec.revision == 42
        assert spec.base == "ubuntu@22.04"


class TestIntegrationSpec:
    def test_local_integration(self) -> None:
        # GIVEN a local integration (no remote_model)
        spec = IntegrationSpec(
            application="my-app",
            endpoint="database",
            remote_application="db-proxy",
            remote_endpoint="backend-database",
        )
        # THEN it is not cross-model
        assert not spec.is_cross_model
        assert spec.remote_model is None

    def test_cross_model_integration(self) -> None:
        # GIVEN a cross-model integration
        spec = IntegrationSpec(
            application="db-proxy",
            endpoint="database",
            remote_model="model-b",
            remote_application="postgresql",
            remote_endpoint="database",
            offer_name="postgresql-offer",
        )
        # THEN it is cross-model
        assert spec.is_cross_model
        assert spec.offer_name == "postgresql-offer"

    def test_resolved_offer_name_explicit(self) -> None:
        # GIVEN an integration with an explicit offer_name
        spec = IntegrationSpec(
            application="a",
            endpoint="e",
            remote_model="m",
            remote_application="b",
            remote_endpoint="e",
            offer_name="custom-offer",
        )
        # THEN resolved_offer_name returns the explicit name
        assert spec.resolved_offer_name() == "custom-offer"

    def test_resolved_offer_name_default(self) -> None:
        # GIVEN an integration without offer_name
        spec = IntegrationSpec(
            application="a",
            endpoint="e",
            remote_model="m",
            remote_application="postgresql",
            remote_endpoint="database",
        )
        # THEN resolved_offer_name defaults to <remote_application>-offer
        assert spec.resolved_offer_name() == "postgresql-offer"


class TestModelSpec:
    def test_defaults(self) -> None:
        # GIVEN a model spec with only applications
        spec = ModelSpec(applications={"my-app": AppSpec(charm="my-charm")})
        # THEN defaults are applied
        assert spec.name is None
        assert spec.arch == "amd64"
        assert spec.platform == "kubernetes"
        assert spec.juju == "3/stable"
        assert spec.controller is None
        assert spec.admin == "admin"
        assert spec.integrations == []


class TestSpecFile:
    def test_duplicate_model_names_raises(self) -> None:
        # GIVEN two models with the same name
        with pytest.raises(ValueError, match="Duplicate model name"):
            SpecFile(
                models=[
                    ModelSpec(name="model-a", applications={"app": AppSpec(charm="c")}),
                    ModelSpec(name="model-a", applications={"app": AppSpec(charm="c")}),
                ]
            )

    def test_model_missing_name_raises(self) -> None:
        # GIVEN a model list entry without a name
        with pytest.raises(ValueError, match="missing a 'name' field"):
            SpecFile(
                models=[
                    ModelSpec(applications={"app": AppSpec(charm="c")}),
                ]
            )

    def test_valid_single_model(self) -> None:
        # GIVEN a valid single-model spec
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="model-a",
                    applications={
                        "my-app": AppSpec(charm="my-charm"),
                        "db": AppSpec(charm="postgresql-k8s"),
                    },
                    integrations=[
                        IntegrationSpec(
                            application="my-app",
                            endpoint="database",
                            remote_application="db",
                            remote_endpoint="database",
                        ),
                    ],
                ),
            ]
        )
        # THEN validation passes
        assert len(spec.models) == 1

    def test_valid_cross_model(self) -> None:
        # GIVEN a valid two-model spec with in-spec CMR
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="model-a",
                    controller="lxd",
                    applications={"db-proxy": AppSpec(charm="pgbouncer-k8s")},
                    integrations=[
                        IntegrationSpec(
                            application="db-proxy",
                            endpoint="database",
                            remote_model="model-b",
                            remote_application="postgresql",
                            remote_endpoint="database",
                            offer_name="postgresql-offer",
                        ),
                    ],
                ),
                ModelSpec(
                    name="model-b",
                    controller="lxd",
                    platform="machine",
                    applications={"postgresql": AppSpec(charm="postgresql")},
                    integrations=[
                        IntegrationSpec(
                            application="postgresql",
                            endpoint="database",
                            remote_model="model-a",
                            remote_application="db-proxy",
                            remote_endpoint="database",
                            offer_name="postgresql-offer",
                        ),
                    ],
                ),
            ]
        )
        assert len(spec.models) == 2

    def test_external_cmr_requires_url(self) -> None:
        # GIVEN a spec with an external CMR missing url
        # WHEN validation runs
        # THEN it raises
        with pytest.raises(ValueError, match="requires a 'url' field"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"my-app": AppSpec(charm="my-charm")},
                        integrations=[
                            IntegrationSpec(
                                application="my-app",
                                endpoint="metrics",
                                remote_model="monitoring",
                                remote_application="prometheus",
                                remote_endpoint="scrape",
                            ),
                        ],
                    ),
                ]
            )

    def test_in_spec_cmr_requires_controller_on_remote(self) -> None:
        # GIVEN a spec where the remote model has no controller
        with pytest.raises(ValueError, match="has no 'controller' set"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_model="model-b",
                                remote_application="rapp",
                                remote_endpoint="re",
                            ),
                        ],
                    ),
                    ModelSpec(
                        name="model-b",
                        applications={"rapp": AppSpec(charm="rc")},
                    ),
                ]
            )

    def test_in_spec_cmr_remote_app_must_exist(self) -> None:
        # GIVEN a spec referencing a remote app that doesn't exist in the remote model
        with pytest.raises(ValueError, match="not defined there"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_model="model-b",
                                remote_application="nonexistent",
                                remote_endpoint="re",
                            ),
                        ],
                    ),
                    ModelSpec(
                        name="model-b",
                        controller="lxd",
                        applications={"rapp": AppSpec(charm="rc")},
                    ),
                ]
            )

    def test_local_integration_app_must_exist(self) -> None:
        # GIVEN a local integration referencing a nonexistent app
        with pytest.raises(ValueError, match="not defined in this model's applications"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_application="nonexistent",
                                remote_endpoint="re",
                            ),
                        ],
                    ),
                ]
            )

    def test_local_integration_local_app_must_exist(self) -> None:
        # GIVEN a local integration where the local app doesn't exist
        with pytest.raises(ValueError, match="not defined in this model's applications"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="nonexistent",
                                endpoint="e",
                                remote_application="app",
                                remote_endpoint="re",
                            ),
                        ],
                    ),
                ]
            )

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        # GIVEN a valid YAML file on disk
        spec_data = {
            "models": [
                {
                    "name": "model-a",
                    "controller": "lxd",
                    "applications": {
                        "my-app": {"charm": "my-charm", "channel": "1/stable"},
                        "db": {"charm": "postgresql-k8s"},
                    },
                    "integrations": [
                        {
                            "application": "my-app",
                            "endpoint": "database",
                            "remote_application": "db",
                            "remote_endpoint": "database",
                        },
                    ],
                },
            ]
        }
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(yaml.dump(spec_data), encoding="utf-8")

        # WHEN loading
        spec = SpecFile.load(spec_path)

        # THEN the spec is valid
        assert "model-a" in spec.models_by_name
        assert spec.models_by_name["model-a"].applications["my-app"].channel == "1/stable"
        assert len(spec.models_by_name["model-a"].integrations) == 1

    def test_cross_model_local_app_must_exist(self) -> None:
        # GIVEN a CMR where the local app doesn't exist
        with pytest.raises(ValueError, match="not defined in this model's applications"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="model-a",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="nonexistent",
                                endpoint="e",
                                remote_model="model-b",
                                remote_application="rapp",
                                remote_endpoint="re",
                            ),
                        ],
                    ),
                    ModelSpec(
                        name="model-b",
                        controller="lxd",
                        applications={"rapp": AppSpec(charm="rc")},
                    ),
                ]
            )

    def test_valid_external_cmr_with_url(self) -> None:
        # GIVEN an external CMR with url provided
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="model-a",
                    applications={"my-app": AppSpec(charm="my-charm")},
                    integrations=[
                        IntegrationSpec(
                            application="my-app",
                            endpoint="metrics",
                            remote_model="monitoring",
                            remote_application="prometheus",
                            remote_endpoint="scrape",
                            url="lxd:admin/monitoring.prometheus-scrape-offer",
                        ),
                    ],
                ),
            ]
        )
        # THEN it is valid
        assert spec.models_by_name["model-a"].integrations[0].url is not None


class TestClassifyIntegrations:
    def test_local_integration(self) -> None:
        # GIVEN a model with a local integration
        model_spec = ModelSpec(
            applications={
                "app-a": AppSpec(charm="ca"),
                "app-b": AppSpec(charm="cb"),
            },
            integrations=[
                IntegrationSpec(
                    application="app-a",
                    endpoint="ep-a",
                    remote_application="app-b",
                    remote_endpoint="ep-b",
                ),
            ],
        )

        # WHEN classifying
        local, cmr = classify_integrations("model-a", model_spec, {"model-a": model_spec})

        # THEN one local, zero cross-model
        assert len(local) == 1
        assert len(cmr) == 0
        ic = next(iter(local))
        assert ic.application_1 == "app-a"
        assert ic.endpoint_1 == "ep-a"

    def test_in_spec_cmr_generates_url(self) -> None:
        # GIVEN two models in spec with a CMR between them
        model_a = ModelSpec(
            controller="lxd",
            applications={"proxy": AppSpec(charm="pgbouncer-k8s")},
            integrations=[
                IntegrationSpec(
                    application="proxy",
                    endpoint="database",
                    remote_model="model-b",
                    remote_application="postgresql",
                    remote_endpoint="database",
                    offer_name="postgresql-offer",
                ),
            ],
        )
        model_b = ModelSpec(
            controller="lxd",
            admin="admin",
            platform="machine",
            applications={"postgresql": AppSpec(charm="postgresql")},
        )
        all_models = {"model-a": model_a, "model-b": model_b}

        # WHEN classifying
        local, cmr = classify_integrations("model-a", model_a, all_models)

        # THEN one cross-model constraint with auto-generated url
        assert len(local) == 0
        assert len(cmr) == 1
        c = cmr[0]
        assert c.remote.model == "model-b"
        assert c.remote.offer_name == "postgresql-offer"
        assert c.remote.url == "lxd:admin/model-b.postgresql-offer"

    def test_external_cmr_uses_provided_url(self) -> None:
        # GIVEN a model with an external CMR
        model_spec = ModelSpec(
            applications={"my-app": AppSpec(charm="my-charm")},
            integrations=[
                IntegrationSpec(
                    application="my-app",
                    endpoint="metrics",
                    remote_model="monitoring",
                    remote_application="prometheus",
                    remote_endpoint="scrape",
                    url="lxd:admin/monitoring.prometheus-scrape-offer",
                ),
            ],
        )

        # WHEN classifying
        local, cmr = classify_integrations("model-a", model_spec, {"model-a": model_spec})

        # THEN one cross-model constraint with the provided url
        assert len(cmr) == 1
        assert cmr[0].remote.url == "lxd:admin/monitoring.prometheus-scrape-offer"

    def test_default_offer_name(self) -> None:
        # GIVEN a CMR without an explicit offer_name
        model_spec = ModelSpec(
            applications={"my-app": AppSpec(charm="my-charm")},
            integrations=[
                IntegrationSpec(
                    application="my-app",
                    endpoint="metrics",
                    remote_model="monitoring",
                    remote_application="prometheus",
                    remote_endpoint="scrape",
                    url="lxd:admin/monitoring.prometheus-offer",
                ),
            ],
        )

        # WHEN classifying
        _, cmr = classify_integrations("model-a", model_spec, {"model-a": model_spec})

        # THEN offer_name defaults to <remote_application>-offer
        assert cmr[0].remote.offer_name == "prometheus-offer"


class TestApplicationsFromSpec:
    def test_converts_app_specs_to_constraints(self) -> None:
        # GIVEN a model spec with applications
        model_spec = ModelSpec(
            applications={
                "my-app": AppSpec(charm="my-charm", channel="1/stable", revision=42, base="ubuntu@22.04"),
                "db": AppSpec(charm="postgresql-k8s"),
            },
        )

        # WHEN converting
        constraints = applications_from_spec(model_spec)

        # THEN all apps are converted with correct fields
        assert len(constraints) == 2
        assert constraints["my-app"].charm == "my-charm"
        assert str(constraints["my-app"].channel) == "1/stable"
        assert constraints["my-app"].revision == 42
        assert constraints["my-app"].base == "ubuntu@22.04"
        assert constraints["db"].charm == "postgresql-k8s"
        assert constraints["db"].channel is None
        assert constraints["db"].revision is None


class TestSpecFileEdgeCases:
    """Probe unusual / invalid inputs to ensure validation catches them early."""

    def test_empty_models_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one model"):
            SpecFile(models=[])

    def test_self_integration_same_endpoint_raises(self) -> None:
        # GIVEN an app that tries to integrate with itself on the same endpoint
        with pytest.raises(ValueError, match="cannot integrate with itself"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="m",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_application="app",
                                remote_endpoint="e",
                            ),
                        ],
                    )
                ]
            )

    def test_self_integration_different_endpoints_allowed(self) -> None:
        # GIVEN an app integrating with itself on different endpoints (e.g. peer)
        # THEN this is allowed - Juju supports peer relations
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="m",
                    applications={"app": AppSpec(charm="c")},
                    integrations=[
                        IntegrationSpec(
                            application="app",
                            endpoint="provides-ep",
                            remote_application="app",
                            remote_endpoint="requires-ep",
                        ),
                    ],
                )
            ]
        )
        assert len(spec.models) == 1

    def test_cmr_to_own_model_raises(self) -> None:
        # GIVEN a CMR whose remote_model is the same as the declaring model
        with pytest.raises(ValueError, match="use a local integration instead"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="m",
                        controller="lxd",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_model="m",
                                remote_application="app",
                                remote_endpoint="e",
                            ),
                        ],
                    )
                ]
            )

    def test_duplicate_cmr_raises(self) -> None:
        # GIVEN the same cross-model integration listed twice
        with pytest.raises(ValueError, match="duplicate cross-model integration"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="m",
                        applications={"app": AppSpec(charm="c")},
                        integrations=[
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_model="ext",
                                remote_application="rapp",
                                remote_endpoint="re",
                                url="ctrl:admin/ext.rapp-offer",
                            ),
                            IntegrationSpec(
                                application="app",
                                endpoint="e",
                                remote_model="ext",
                                remote_application="rapp",
                                remote_endpoint="re",
                                url="ctrl:admin/ext.rapp-offer",
                            ),
                        ],
                    )
                ]
            )

    def test_duplicate_local_integration_rejected(self) -> None:
        # GIVEN the same local integration listed twice
        # THEN spec parsing raises a validation error
        with pytest.raises(ValueError, match="duplicate local integration"):
            SpecFile(
                models=[
                    ModelSpec(
                        name="m",
                        applications={"a": AppSpec(charm="ca"), "b": AppSpec(charm="cb")},
                        integrations=[
                            IntegrationSpec(application="a", endpoint="e", remote_application="b", remote_endpoint="f"),
                            IntegrationSpec(application="a", endpoint="e", remote_application="b", remote_endpoint="f"),
                        ],
                    )
                ]
            )

    def test_in_spec_cmr_explicit_url_does_not_require_controller(self) -> None:
        # GIVEN an in-spec CMR where the remote model has no controller but url is provided
        # THEN it is valid - the explicit url supersedes auto-generation
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="m-a",
                    applications={"app": AppSpec(charm="c")},
                    integrations=[
                        IntegrationSpec(
                            application="app",
                            endpoint="e",
                            remote_model="m-b",
                            remote_application="rapp",
                            remote_endpoint="re",
                            url="my-ctrl:admin/m-b.rapp-offer",
                        ),
                    ],
                ),
                ModelSpec(
                    name="m-b",
                    applications={"rapp": AppSpec(charm="rc")},
                    # no controller - but url is explicit so this should be fine
                ),
            ]
        )
        assert spec.models_by_name["m-a"].integrations[0].url == "my-ctrl:admin/m-b.rapp-offer"

    def test_in_spec_cmr_explicit_url_wins_over_auto_generate(self) -> None:
        # GIVEN an in-spec CMR with both a remote model that has a controller AND an explicit url
        model_a = ModelSpec(
            controller="lxd",
            applications={"app": AppSpec(charm="c")},
            integrations=[
                IntegrationSpec(
                    application="app",
                    endpoint="e",
                    remote_model="m-b",
                    remote_application="rapp",
                    remote_endpoint="re",
                    url="EXPLICIT_URL",
                ),
            ],
        )
        model_b = ModelSpec(controller="other-ctrl", applications={"rapp": AppSpec(charm="rc")})
        _, cmr = classify_integrations("m-a", model_a, {"m-a": model_a, "m-b": model_b})
        # THEN the explicit url is used, not the auto-generated one
        assert cmr[0].remote.url == "EXPLICIT_URL"

    def test_empty_applications_rejected(self) -> None:
        # GIVEN a model with no applications
        # THEN spec parsing raises a validation error
        with pytest.raises(ValueError, match="at least one application"):
            SpecFile(models=[ModelSpec(name="m", applications={})])

    def test_converts_app_specs_to_constraints(self) -> None:
        # GIVEN a model spec with applications
        model_spec = ModelSpec(
            applications={
                "my-app": AppSpec(charm="my-charm", channel="1/stable", revision=42, base="ubuntu@22.04"),
                "db": AppSpec(charm="postgresql-k8s"),
            },
        )

        # WHEN converting
        constraints = applications_from_spec(model_spec)

        # THEN all apps are converted with correct fields
        assert len(constraints) == 2
        assert constraints["my-app"].charm == "my-charm"
        assert str(constraints["my-app"].channel) == "1/stable"
        assert constraints["my-app"].revision == 42
        assert constraints["my-app"].base == "ubuntu@22.04"
        assert constraints["db"].charm == "postgresql-k8s"
        assert constraints["db"].channel is None
        assert constraints["db"].revision is None
