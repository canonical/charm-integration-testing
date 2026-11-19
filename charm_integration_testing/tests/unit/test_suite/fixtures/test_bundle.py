# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import sys
from pathlib import Path
from textwrap import dedent
from xml.etree import ElementTree

import pytest

pytest_plugins = ["pytester"]

_FIXTURES = """
import logging
import pytest
from test_suite.conftest import (
    pytest_addoption, target_bundle, neighbor_bundle, bundle_mermaid_output,
    target_application, target_endpoint, neighbor_application, neighbor_endpoint,
    target_charm, neighbor_charm, target_channel, target_revision, target_series,
    target_platform, target_arch, neighbor_platform, neighbor_arch, target_downgrade_revision,
)

pytest_plugins = [
    "test_suite.fixtures.bundle",
    "test_suite.fixtures.controller_spec",
    "test_suite.scheduler.plugin",
]

@pytest.fixture(scope="session")
def logger():
    return logging.getLogger("bundle-resolution")

@pytest.fixture(scope="session")
def git_client():
    return object()
"""

_CATALOG = """
from bundle_builder_x import (
    BundleBuilder, Charm, CharmChannel, CharmEndpoint, CharmhubClient,
    CharmReleaseNotFoundException, EndpointType,
)
from bundle_builder_x.charm import EndpointScope
from juju import JujuVersion

class Catalog(CharmhubClient):
    def __init__(self, cmr):
        super().__init__()
        endpoint = CharmEndpoint(
            type=EndpointType.REQUIRES,
            interface="juju-info",
            optional=False,
            scope=EndpointScope.GLOBAL if cmr else EndpointScope.CONTAINER,
        )
        self.charms = [
            Charm(
                name="nrpe", channel="2/stable", revision=100,
                ubuntu_arch="amd64", ubuntu_version="24.04", subordinate=not cmr,
                endpoints={"general-info": endpoint}, platforms=["machine"],
            ),
            Charm(
                name="nrpe", channel="1/stable", revision=200,
                ubuntu_arch="amd64", ubuntu_version="22.04", subordinate=not cmr,
                endpoints={"general-info": endpoint}, platforms=["machine"],
            ),
            Charm(
                name="ubuntu", channel="latest/stable", revision=300,
                ubuntu_arch="amd64", ubuntu_version="22.04", platforms=["machine"],
                endpoints={"juju-info": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="juju-info",
                )},
            ),
        ]

    def charm_from_store(
        self, charm_name, ubuntu_arch, juju_version=None, platform=None,
        charm_track=None, charm_risk=None, charm_revision=None,
        ubuntu_version=None, charm_branch=None,
    ):
        self.last_platform = platform
        self.last_juju_version = juju_version
        self.last_revision = charm_revision
        for charm in self.charms:
            if (
                charm.name == charm_name
                and (charm_track is None or charm.channel.explicit_track == (charm_track or "latest"))
                and (charm_risk is None or charm.channel.risk == charm_risk)
                and (charm_revision is None or charm.revision == charm_revision)
                and (ubuntu_version is None or charm.ubuntu_version == ubuntu_version)
            ):
                return charm
        raise CharmReleaseNotFoundException("No matching synthetic release")

    def get_charm_channels(self, charm_name):
        return sorted({c.channel for c in self.charms if c.name == charm_name})

    def find_charms(self, provides=None, requires=None, platform=None):
        return set()

class CountingBuilder(BundleBuilder):
    builds = 0

    def build(self, spec):
        self.builds += 1
        return super().build(spec)

@pytest.fixture(scope="session")
def juju_cli_version():
    return JujuVersion(3, 6, 29)

@pytest.fixture(scope="session")
def charmhub_client(is_cmr_test):
    return Catalog(is_cmr_test)

@pytest.fixture(scope="session")
def bundle_builder(charmhub_client):
    return CountingBuilder(charmhub_client=charmhub_client)
"""


@pytest.mark.parametrize("cmr", [False, True], ids=["solver-alternative", "cmr"])
def test_target_uses_the_exported_solution(pytester: pytest.Pytester, cmr: bool) -> None:
    # GIVEN the default target base is incompatible with its principal
    pytester.makeconftest(_FIXTURES + _CATALOG)
    pytester.makepyfile(
        test_00_selection="""
        def test_selection_before_export(target_resolved_charm, charmhub_client):
            default = charmhub_client.charm_from_store(charm_name="nrpe", ubuntu_arch="amd64")
            assert default.revision == 100
            assert target_resolved_charm.revision == 200
            assert str(target_resolved_charm.channel) == "1/stable"
            assert target_resolved_charm.ubuntu_version == "22.04"
        """,
        test_01_export="from test_suite.test_build_bundle import test_build_bundle",
        test_02_verify="""
        import yaml

        def test_export_matches_target(target_resolved_charm, target_bundle, bundle_builder, bundle_solution):
            app = next(yaml.safe_load_all(target_bundle.read_text()))["applications"]["target"]
            assert app["revision"] == target_resolved_charm.revision == 200
            assert app["channel"] == str(target_resolved_charm.channel)
            assert app["base"] == "ubuntu@" + target_resolved_charm.ubuntu_version
            assert bundle_builder.builds == 1
        """,
        test_03_restore="""
        import logging
        from test_suite import test_upgrade_charm as module

        class JujuStub:
            logger = logging.getLogger("restore")
            refreshed = None

            def refresh_application(self, **kwargs):
                self.refreshed = kwargs

            def wait_for_application_revision(self, **kwargs):
                pass

            def multi_model_idle_for_period(self, *args, **kwargs):
                pass

            def application_revision(self, **kwargs):
                return self.refreshed["revision"]

            def validate_model(self, **kwargs):
                pass

        def test_restore_bundle_release(target_resolved_charm_for_refresh, target_model_ref, neighbor_model_ref):
            juju = JujuStub()
            module.test_upgrade_charm(
                juju_client=juju,
                target_downgrade_revision=50,
                target_model_ref=target_model_ref,
                neighbor_model_ref=neighbor_model_ref,
                target_application="target",
                target_resolved_charm_for_refresh=target_resolved_charm_for_refresh,
            )
            assert juju.refreshed["revision"] == 200
            assert juju.refreshed["channel"] == "1/stable"
        """,
        test_04_source="""
        import pytest
        from test_suite.fixtures.charm_source import target_charm_source

        class DownloadProbe:
            def download_charm(self, name, revision, destination):
                raise RuntimeError(f"Download requested: {name} revision {revision}")

        @pytest.fixture(scope="session")
        def juju_client():
            return DownloadProbe()

        def test_lint_source_downloads_selected_revision(request):
            with pytest.raises(RuntimeError, match="Download requested: nrpe revision 200"):
                request.getfixturevalue("target_charm_source")
        """,
    )
    args = [
        "--target-charm",
        "nrpe",
        "--target-application",
        "target",
        "--target-endpoint",
        "general-info",
        "--neighbor-charm",
        "ubuntu",
        "--neighbor-application",
        "neighbor",
        "--neighbor-endpoint",
        "juju-info",
        "--target-platform",
        "machine",
        "--current-state",
        "no_bundle",
        "--prefix",
        "bundle-test",
        "--mermaid-output",
        str(pytester.path / "bundle.mmd"),
    ]
    if cmr:
        args += ["--neighbor-cloud", "localhost", "--target-channel", "1/stable", "--target-revision", "200"]

    # WHEN selection runs before bundle export, using real fixtures and the real solver
    result = pytester.runpytest(*args, "-q")

    # THEN selection and exported deployment both use the same release, with one build
    result.assert_outcomes(passed=5)
    if cmr:
        assert (pytester.path / "generated-neighbor-bundle.yaml").is_file()


@pytest.mark.parametrize(
    ("selectors", "expected_revision", "requested_revision"),
    [
        pytest.param("    revision: 200\n    channel: 1/stable\n    base: ubuntu@22.04\n", 200, 200, id="pinned"),
        pytest.param("    channel: 1/stable\n    base: ubuntu@22.04\n", 200, None, id="channel-and-base"),
        pytest.param("", 100, None, id="charm-only"),
    ],
)
def test_resumed_run_uses_saved_target_selectors(
    pytester: pytest.Pytester, selectors: str, expected_revision: int, requested_revision: int | None
) -> None:
    # GIVEN a saved bundle with an unpinned neighbor and optional target selectors
    bundle = pytester.path / "saved.yaml"
    bundle.write_text(
        "applications:\n  target:\n    charm: nrpe\n" + selectors + "  neighbor:\n    charm: ubuntu\n"
        "---\napplications:\n  target:\n    offers: {}\n"
    )
    pytester.makeconftest(
        _FIXTURES
        + _CATALOG
        + dedent("""
        @pytest.fixture(scope="session")
        def bundle_solution():
            pytest.fail("A resumed run must not synthesize a replacement bundle")
        """)
    )
    pytester.makepyfile(
        f"""
        from bundle_builder_x import JujuVersion

        def test_selected(target_resolved_charm, charmhub_client):
            assert target_resolved_charm.revision == {expected_revision}
            assert charmhub_client.last_revision == {requested_revision!r}
            assert charmhub_client.last_platform == "machine"
            assert charmhub_client.last_juju_version == JujuVersion.parse("3.6.29")

        def test_restore(target_resolved_charm_for_refresh):
            assert target_resolved_charm_for_refresh.revision == {expected_revision}
        """
    )
    if requested_revision is None:
        pytester.makepyfile(
            test_downgrade="""
            def test_downgrade_requires_original_revision(target_downgrade_revision):
                raise AssertionError("Downgrade must not run without an original revision")
            """
        )
    original = bundle.read_text()

    # WHEN resuming with selectors that would otherwise choose the default release
    result = pytester.runpytest(
        "--current-state",
        "deployed",
        "--target-controller",
        "existing",
        "--target-model",
        "existing",
        "--target-application",
        "target",
        "--target-platform",
        "machine",
        "--target-bundle",
        str(bundle),
        "-q",
        "-rs",
    )

    # THEN lookup honors saved selectors, but refresh cannot guess an original revision
    if requested_revision is None:
        result.assert_outcomes(passed=1, skipped=2)
        reason = "The saved bundle has no target revision to restore after downgrade."
        result.stdout.fnmatch_lines(
            [
                f"SKIPPED *test_downgrade.py:*{reason}",
                f"SKIPPED *test_resumed_run_uses_saved_target_selectors.py:*{reason}",
            ]
        )
    else:
        result.assert_outcomes(passed=2)
    assert bundle.read_text() == original


@pytest.mark.parametrize("revision", ["true", "-1", "'200'"])
def test_resumed_bundle_rejects_invalid_revision(pytester: pytest.Pytester, revision: str) -> None:
    # GIVEN an existing bundle with a malformed explicit revision
    bundle: Path = pytester.path / "saved.yaml"
    bundle.write_text(f"applications:\n  target:\n    charm: nrpe\n    revision: {revision}\n")
    pytester.makeconftest(_FIXTURES + _CATALOG)
    pytester.makepyfile(
        """
        def test_selected(target_resolved_charm):
            pass
        """
    )

    # WHEN resolving the target, THEN invalid provenance fails rather than falling back
    result = pytester.runpytest(
        "--current-state",
        "deployed",
        "--target-controller",
        "existing",
        "--target-model",
        "existing",
        "--target-application",
        "target",
        "--target-bundle",
        str(bundle),
        "-q",
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*ValidationError*"])


def test_failed_synthesis_is_shared_and_reported_by_build_test(pytester: pytest.Pytester) -> None:
    # GIVEN an explicit revision absent from the catalog
    pytester.makeconftest(_FIXTURES + _CATALOG)
    pytester.makepyfile(
        test_00_selection="""
        import pytest
        from bundle_builder_x import UncompletableBundleError

        def test_target_cannot_resolve(request):
            with pytest.raises(UncompletableBundleError):
                request.getfixturevalue("target_resolved_charm")
        """,
        test_01_export="from test_suite.test_build_bundle import test_build_bundle",
        test_02_verify="""
        def test_only_one_attempt(bundle_builder):
            assert bundle_builder.builds == 1
        """,
    )
    junit = pytester.path / "junit.xml"

    # WHEN selection fails before the build test runs
    result = pytester.runpytest(
        "--target-charm",
        "nrpe",
        "--target-application",
        "target",
        "--target-endpoint",
        "general-info",
        "--neighbor-charm",
        "ubuntu",
        "--neighbor-application",
        "neighbor",
        "--neighbor-endpoint",
        "juju-info",
        "--target-revision",
        "999",
        "--target-platform",
        "machine",
        "--current-state",
        "no_bundle",
        "--prefix",
        "bundle-test",
        "--mermaid-output",
        str(pytester.path / "bundle.mmd"),
        f"--junitxml={junit}",
        "-q",
    )

    # THEN the build keeps its call-phase failure and does not retry synthesis
    result.assert_outcomes(passed=2, failed=1)
    build = next(case for case in ElementTree.parse(junit).iter("testcase") if case.get("name") == "test_build_bundle")
    failure = build.find("failure")
    assert failure is not None
    assert "UncompletableBundleError" in failure.get("message", "")
    assert build.find("error") is None


@pytest.mark.parametrize("lint_before_build", [True, False], ids=["lint-first", "build-first"])
def test_failed_synthesis_skips_only_charmlint(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, lint_before_build: bool
) -> None:
    # GIVEN an unsatisfiable bundle and lint/download probes that must not run
    executable = pytester.path / "charmlint"
    executable.write_text(f"#!{sys.executable}\nraise AssertionError('charmlint must not run')\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(pytester.path))
    pytester.makeconftest(
        _FIXTURES
        + _CATALOG
        + dedent("""
        from test_suite.fixtures.charm_source import target_charm_source

        @pytest.fixture(scope="session")
        def juju_client():
            pytest.fail("Juju must not be acquired for an unresolved target")
        """)
    )
    lint_module = "test_00_lint" if lint_before_build else "test_02_lint"
    pytester.makepyfile(
        **{lint_module: "from test_suite.test_charmlint import test_charmlint, test_charmlint_strict"},
        test_01_export="from test_suite.test_build_bundle import test_build_bundle",
        test_03_verify="""
        def test_only_one_attempt(bundle_builder):
            assert bundle_builder.builds == 1
        """,
    )
    junit = pytester.path / "junit.xml"

    # WHEN both lint cases and the production build test request the same solution
    result = pytester.runpytest(
        "--target-charm",
        "nrpe",
        "--target-application",
        "target",
        "--target-endpoint",
        "general-info",
        "--neighbor-charm",
        "ubuntu",
        "--neighbor-application",
        "neighbor",
        "--neighbor-endpoint",
        "juju-info",
        "--target-revision",
        "999",
        "--target-platform",
        "machine",
        "--current-state",
        "no_bundle",
        "--prefix",
        "bundle-test",
        "--mermaid-output",
        str(pytester.path / "bundle.mmd"),
        f"--junitxml={junit}",
        "-q",
        "-rs",
    )

    # THEN only linting skips, preserving the solver reason and original build failure
    result.assert_outcomes(passed=1, failed=1, skipped=2)
    cases = {case.get("name"): case for case in ElementTree.parse(junit).iter("testcase")}
    for name in ("test_charmlint", "test_charmlint_strict"):
        skipped = cases[name].find("skipped")
        assert skipped is not None
        assert "Cannot resolve target charm for linting:" in skipped.get("message", "")
        assert "Could not build a complete valid bundle:" in skipped.get("message", "")
        assert "No matching synthetic release" in skipped.get("message", "")
    failure = cases["test_build_bundle"].find("failure")
    assert failure is not None
    assert "UncompletableBundleError" in failure.get("message", "")
    assert "charmlint must not run" not in result.stdout.str()
