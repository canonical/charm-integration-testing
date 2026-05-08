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

"""Unit tests for Bundle.export() and Solution.export_mermaid()."""

import yaml

from bundle_builder_x.bundle import (
    Application,
    ApplicationEndpoint,
    Bundle,
    Integration,
    Solution,
)
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint] | None = None) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel(track="1", risk="stable", branch=""),
        revision=42,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
    )


def _make_bundle(**kwargs: object) -> Bundle:
    defaults: dict[str, object] = {
        "applications": {},
        "integrations": set(),
        "cross_model_integrations": [],
        "platform": "kubernetes",
        "arch": "amd64",
        "juju_version": _JUJU,
    }
    defaults.update(kwargs)
    return Bundle(**defaults)


class TestBundleExport:
    def test_minimal_single_app(self) -> None:
        # GIVEN a bundle with one application, no integrations
        charm = _make_charm("my-app")
        bundle = _make_bundle(applications={"my-app": Application(charm=charm)})

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN the application appears with correct fields
        app = exported["applications"]["my-app"]
        assert app["charm"] == "my-app"
        assert app["channel"] == "1/stable"
        assert app["revision"] == 42
        assert app["base"] == "ubuntu@22.04"
        assert app["scale"] == 1
        assert app["trust"] is True

    def test_kubernetes_uses_scale_key(self) -> None:
        # GIVEN a kubernetes bundle
        bundle = _make_bundle(
            platform="kubernetes",
            applications={"app": Application(charm=_make_charm("app"))},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN scale key is used, not num_units
        assert "scale" in exported["applications"]["app"]
        assert "num_units" not in exported["applications"]["app"]

    def test_machine_uses_num_units_key(self) -> None:
        # GIVEN a machine bundle
        bundle = _make_bundle(
            platform="machine",
            applications={"app": Application(charm=_make_charm("app"))},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN num_units key is used, not scale
        assert "num_units" in exported["applications"]["app"]
        assert "scale" not in exported["applications"]["app"]

    def test_unsupported_platform_raises(self) -> None:
        # GIVEN a bundle with an invalid platform
        bundle = _make_bundle(
            platform="lxd",
            applications={"app": Application(charm=_make_charm("app"))},
        )

        # WHEN/THEN exporting raises
        import pytest

        with pytest.raises(ValueError, match="Unsupported platform"):
            bundle.export()

    def test_config_options_included(self) -> None:
        # GIVEN an application with config
        charm = _make_charm("app")
        bundle = _make_bundle(
            applications={"app": Application(charm=charm, config={"key": "value", "flag": True})},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN config appears under options
        assert exported["applications"]["app"]["options"]["key"] == "value"
        assert exported["applications"]["app"]["options"]["flag"] is True

    def test_none_config_excluded(self) -> None:
        # GIVEN an application with a None config value
        charm = _make_charm("app")
        bundle = _make_bundle(
            applications={"app": Application(charm=charm, config={"key": None})},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN None values are excluded from options
        assert exported["applications"]["app"]["options"] == {}

    def test_resources_included(self) -> None:
        # GIVEN an application with resources
        charm = _make_charm("app")
        bundle = _make_bundle(
            applications={"app": Application(charm=charm, resources={"my-image": "ghcr.io/foo:latest"})},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN resources appear under the resources key
        assert exported["applications"]["app"]["resources"]["my-image"] == "ghcr.io/foo:latest"

    def test_empty_resources_omitted(self) -> None:
        # GIVEN an application with no resources
        charm = _make_charm("app")
        bundle = _make_bundle(
            applications={"app": Application(charm=charm)},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN no resources key is present
        assert "resources" not in exported["applications"]["app"]

    def test_local_relations_sorted(self) -> None:
        # GIVEN a bundle with two local integrations
        provider = _make_charm(
            "pg",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        requirer = _make_charm(
            "app",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        other = _make_charm(
            "other",
            endpoints={"backend": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        bundle = _make_bundle(
            applications={
                "pg": Application(charm=provider),
                "app": Application(charm=requirer),
                "other": Application(charm=other),
            },
            integrations={
                Integration.create(
                    ApplicationEndpoint(application="pg", endpoint="database"),
                    ApplicationEndpoint(application="other", endpoint="backend"),
                ),
                Integration.create(
                    ApplicationEndpoint(application="pg", endpoint="database"),
                    ApplicationEndpoint(application="app", endpoint="db"),
                ),
            },
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN relations are present and sorted
        relations = exported["relations"]
        assert len(relations) == 2
        # Relations should be deterministically sorted
        assert relations == sorted(relations)

    def test_bundle_key_matches_platform_k8s(self) -> None:
        # GIVEN a kubernetes bundle
        bundle = _make_bundle(
            platform="kubernetes",
            applications={"app": Application(charm=_make_charm("app"))},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN the bundle key is 'kubernetes'
        assert exported["bundle"] == "kubernetes"

    def test_bundle_key_is_skipped_for_platform_machine(self) -> None:
        # GIVEN a machine bundle
        bundle = _make_bundle(
            platform="machine",
            applications={"app": Application(charm=_make_charm("app"))},
        )

        # WHEN exporting
        exported = yaml.safe_load(bundle.export())

        # THEN the bundle key is absent (it is only emitted for kubernetes bundles)
        assert "bundle" not in exported

    def test_export_round_trip_is_valid_yaml(self) -> None:
        # GIVEN a moderately complex bundle
        pg = _make_charm(
            "pg",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        app = _make_charm(
            "app",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        bundle = _make_bundle(
            model="test-model",
            applications={
                "pg": Application(charm=pg, config={"max_connections": 100}),
                "app": Application(charm=app),
            },
            integrations={
                Integration.create(
                    ApplicationEndpoint(application="pg", endpoint="database"),
                    ApplicationEndpoint(application="app", endpoint="db"),
                ),
            },
        )

        # WHEN exporting and re-parsing
        exported_str = bundle.export()
        reparsed = yaml.safe_load(exported_str)

        # THEN the result is a valid dict with all expected top-level keys
        assert isinstance(reparsed, dict)
        assert "applications" in reparsed
        assert "relations" in reparsed
        assert "bundle" in reparsed


class TestSolutionExportMermaid:
    def test_single_model_subgraph(self) -> None:
        # GIVEN a single-model solution
        charm = _make_charm("my-charm")
        bundle = _make_bundle(
            model="test-model",
            applications={"my-app": Application(charm=charm)},
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting
        mermaid = solution.export_mermaid()

        # THEN it contains the subgraph and node
        assert "subgraph test-model" in mermaid
        assert "test-model__my-app" in mermaid
        assert "graph TB" in mermaid

    def test_charm_name_shown_when_different_from_app(self) -> None:
        # GIVEN an application name that differs from the charm name
        charm = _make_charm("postgresql-k8s")
        bundle = _make_bundle(
            model="m",
            applications={"pg": Application(charm=charm)},
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting
        mermaid = solution.export_mermaid()

        # THEN the charm name appears in parentheses
        assert "(postgresql-k8s)" in mermaid

    def test_charm_name_hidden_when_same_as_app(self) -> None:
        # GIVEN an application name that matches the charm name
        charm = _make_charm("my-charm")
        bundle = _make_bundle(
            model="m",
            applications={"my-charm": Application(charm=charm)},
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting
        mermaid = solution.export_mermaid()

        # THEN no parenthesized charm name
        assert "(my-charm)" not in mermaid

    def test_local_integration_arrow(self) -> None:
        # GIVEN two apps with a local integration
        provider = _make_charm(
            "pg",
            endpoints={"database": CharmEndpoint(type=EndpointType.PROVIDES, interface="postgresql")},
        )
        requirer = _make_charm(
            "app",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql")},
        )
        bundle = _make_bundle(
            model="m",
            applications={
                "pg": Application(charm=provider),
                "app": Application(charm=requirer),
            },
            integrations={
                Integration.create(
                    ApplicationEndpoint(application="pg", endpoint="database"),
                    ApplicationEndpoint(application="app", endpoint="db"),
                ),
            },
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting
        mermaid = solution.export_mermaid()

        # THEN a solid arrow connects the two apps
        assert "-->|" in mermaid
        assert "m__pg" in mermaid
        assert "m__app" in mermaid

    def test_markdown_wrapping(self) -> None:
        # GIVEN a solution
        bundle = _make_bundle(
            model="m",
            applications={"app": Application(charm=_make_charm("app"))},
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting with markdown=True
        mermaid = solution.export_mermaid(markdown=True)

        # THEN it is wrapped in mermaid code fences
        assert mermaid.startswith("```mermaid\n")
        assert mermaid.endswith("```\n")

    def test_no_markdown_wrapping(self) -> None:
        # GIVEN a solution
        bundle = _make_bundle(
            model="m",
            applications={"app": Application(charm=_make_charm("app"))},
        )
        solution = Solution(bundles=[bundle])

        # WHEN exporting with markdown=False (default)
        mermaid = solution.export_mermaid()

        # THEN no code fences
        assert not mermaid.startswith("```")
