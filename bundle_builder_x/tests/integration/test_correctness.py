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

"""Integration tests: end-to-end spec-to-solution via real Charmhub + solver.

These tests use the real Charmhub API and real override files.
They validate that the full pipeline (spec -> domain -> solve -> extract)
produces correct, deployable bundles.
"""

import yaml

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.snapstore import SnapstoreClient
from bundle_builder_x.spec import SpecFile


def test_independent_charm_produces_minimal_bundle(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a spec with a single independent charm (all endpoints optional)
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {"pg": {"charm": "postgresql-k8s", "channel": "14/stable"}},
                }
            ]
        }
    )

    # WHEN building
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)

    # THEN the solution contains one bundle
    assert len(solution.bundles) == 1
    bundle = solution.bundles[0]
    # AND the pinned application is present
    assert "pg" in bundle.applications
    assert bundle.applications["pg"].charm.name == "postgresql-k8s"
    # AND the bundle exports valid YAML
    exported = yaml.safe_load(bundle.export())
    assert "pg" in exported["applications"]


def test_dependent_charm_expands_domain(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a spec with kratos, which requires postgresql-k8s via pg-database
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {
                        "kratos": {"charm": "kratos"},
                    },
                }
            ]
        }
    )

    # WHEN building
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)

    # THEN the solution contains kratos plus auto-discovered dependencies
    bundle = solution.bundles[0]
    app_names = set(bundle.applications.keys())
    assert "kratos" in app_names
    # kratos requires pg-database, so postgresql-k8s (or equivalent) should be pulled in
    assert len(app_names) > 1
    # AND there is at least one integration
    assert len(bundle.integrations) >= 1


def test_explicit_integration_appears_in_output(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a spec with an explicit integration between two charms
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {
                        "pg": {"charm": "postgresql-k8s", "channel": "14/stable"},
                        "pgbouncer": {"charm": "pgbouncer-k8s", "channel": "1/stable"},
                    },
                    "integrations": [
                        {
                            "application": "pgbouncer",
                            "endpoint": "backend-database",
                            "remote_application": "pg",
                            "remote_endpoint": "database",
                        }
                    ],
                }
            ]
        }
    )

    # WHEN building
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)

    # THEN the explicit integration is in the output
    bundle = solution.bundles[0]
    integration_endpoints = set()
    for integration in bundle.integrations:
        for ep in integration:
            integration_endpoints.add(f"{ep.application}:{ep.endpoint}")
    assert "pgbouncer:backend-database" in integration_endpoints
    assert "pg:database" in integration_endpoints


def test_multi_model_spec_produces_separate_bundles(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a spec with two models
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "model-a",
                    "platform": "kubernetes",
                    "applications": {"pg": {"charm": "postgresql-k8s", "channel": "14/stable"}},
                },
                {
                    "name": "model-b",
                    "platform": "kubernetes",
                    "applications": {"mysql": {"charm": "mysql-k8s", "channel": "8.0/stable"}},
                },
            ]
        }
    )

    # WHEN building
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)

    # THEN we get two bundles
    assert len(solution.bundles) == 2
    model_names = {b.model for b in solution.bundles}
    assert model_names == {"model-a", "model-b"}
    # AND each bundle has the correct application
    bundle_a = next(b for b in solution.bundles if b.model == "model-a")
    bundle_b = next(b for b in solution.bundles if b.model == "model-b")
    assert "pg" in bundle_a.applications
    assert "mysql" in bundle_b.applications


def test_exported_yaml_is_valid(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a spec with integrations
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {
                        "pg": {"charm": "postgresql-k8s", "channel": "14/stable"},
                        "pgbouncer": {"charm": "pgbouncer-k8s", "channel": "1/stable"},
                    },
                    "integrations": [
                        {
                            "application": "pgbouncer",
                            "endpoint": "backend-database",
                            "remote_application": "pg",
                            "remote_endpoint": "database",
                        }
                    ],
                }
            ]
        }
    )

    # WHEN building and exporting
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)
    bundle_yaml = solution.bundles[0].export()

    # THEN the exported YAML is valid and contains expected structure
    parsed = yaml.safe_load(bundle_yaml)
    assert isinstance(parsed, dict)
    assert "applications" in parsed
    assert "relations" in parsed
    assert "bundle" in parsed
    assert parsed["bundle"] == "kubernetes"
    # AND every application has required fields
    for app_name, app_data in parsed["applications"].items():
        assert "charm" in app_data, f"Application {app_name} missing 'charm'"
        assert "channel" in app_data, f"Application {app_name} missing 'channel'"
        assert "revision" in app_data, f"Application {app_name} missing 'revision'"
        assert "base" in app_data, f"Application {app_name} missing 'base'"


def test_mermaid_export_contains_all_models(
    charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN a two-model spec
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "infra",
                    "platform": "kubernetes",
                    "applications": {"pg": {"charm": "postgresql-k8s", "channel": "14/stable"}},
                },
                {
                    "name": "app-tier",
                    "platform": "kubernetes",
                    "applications": {"mysql": {"charm": "mysql-k8s", "channel": "8.0/stable"}},
                },
            ]
        }
    )

    # WHEN building and exporting mermaid
    builder = BundleBuilder(charmhub_client=charmhub_client, snapstore_client=snapstore_client)
    solution = builder.build(spec)
    mermaid = solution.export_mermaid()

    # THEN both models appear as subgraphs
    assert "subgraph infra" in mermaid
    assert "subgraph app-tier" in mermaid
