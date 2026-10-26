# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from ci_runner.config import Config, Scenario, load_config
from ci_runner.reporting import junit_results
from ci_runner.runner import discover, pytest_command, run_scenarios
from pydantic import ValidationError

from bundle_builder_x import Charm, CharmChannel, CharmhubClient
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.juju_version import JujuVersion


def charm(name: str, role: EndpointType) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel.model_validate("latest/stable"),
        revision=-1,
        local_path="/tmp/local build.charm" if name == "target" else None,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={"db": CharmEndpoint(type=role, interface="db")},
        platforms=["kubernetes"],
    )


class DiscoveryStub(CharmhubClient):
    def find_charms(
        self, provides: str | None = None, requires: str | None = None, platform: str | None = None
    ) -> set[str]:
        assert requires == "db" and provides is None and platform == "kubernetes"
        return {"client-b", "client-a"}

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None = None,
        platform: str | None = None,
        charm_track: str | None = None,
        charm_risk: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
        charm_branch: str | None = None,
    ) -> Charm:
        return charm(charm_name, EndpointType.REQUIRES)


def test_discovery_is_deterministic_and_filterable() -> None:
    target = charm("target", EndpointType.PROVIDES)
    client = DiscoveryStub()
    assert [s.neighbor for s in discover(client, target, Config(arch="amd64"), "kubernetes")] == [
        "client-a",
        "client-b",
    ]
    assert [s.neighbor for s in discover(client, target, Config(neighbors=["client-b"]), "kubernetes")] == ["client-b"]
    assert discover(client, target, Config(endpoints=["missing"]), "kubernetes") == []


def test_config_rejects_typos_and_empty_selections() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"scenario": []})
    with pytest.raises(ValidationError):
        Config(scenarios=[])
    with pytest.raises(ValidationError):
        Config(observer_execution_id=0)


def test_config_resolves_paths_relative_to_file(tmp_path: Path) -> None:
    (tmp_path / "payload").write_text("data")
    (tmp_path / "overrides").mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"resources": {"payload": "./payload"}, "overrides": "./overrides"}))
    config = load_config(config_path)
    assert config.resources == {"payload": str(tmp_path / "payload")}
    assert config.overrides == tmp_path / "overrides"


def test_pytest_argv_keeps_local_artifact_and_overrides(tmp_path: Path) -> None:
    target = charm("target", EndpointType.PROVIDES)
    scenario = Scenario(endpoint="db", neighbor="client", neighbor_endpoint="db", pytest_args=["-m", "not slow"])
    config = Config(arch="amd64", pytest_args=["-k", "test_pod_deletion", "--target-revision", "42"])
    argv = pytest_command(target, scenario, config, "local-k8s", "kubernetes", tmp_path)
    assert argv[argv.index("--target-charm-path") + 1] == "/tmp/local build.charm"
    assert argv[argv.index("-k") + 1] == "test_pod_deletion"
    assert argv[argv.index("-m", 3) + 1] == "not slow"
    assert argv[argv.index("--target-bundle") + 1] == str(tmp_path / "bundle.yaml")
    revision_options = [index for index, arg in enumerate(argv) if arg == "--target-revision"]
    assert argv[revision_options[-1] + 1] == "default"
    assert not any("test_pod_deletion.py" in arg for arg in argv)


@pytest.mark.parametrize("first_exit", [0, 1, 5], ids=["pass", "failure", "no-tests"])
def test_all_scenarios_run_without_retrying_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_exit: int
) -> None:
    calls: list[list[str]] = []

    def execute(argv: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        report = Path(argv[argv.index("--junitxml") + 1])
        report.write_text('<testsuites><testsuite><testcase name="test_deploy"/></testsuite></testsuites>')
        return subprocess.CompletedProcess(argv, first_exit if len(calls) == 1 else 0)

    monkeypatch.setattr("ci_runner.runner.subprocess.run", execute)
    scenarios = [Scenario(endpoint="db", neighbor=f"client-{name}", neighbor_endpoint="db") for name in ("a", "b")]
    status = run_scenarios(charm("target", EndpointType.PROVIDES), scenarios, Config(), "k8s", "kubernetes", tmp_path)
    assert status == (0 if first_exit == 0 else 1)
    assert len(calls) == 2


def test_missing_junit_is_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("ci_runner.runner.subprocess.run", execute)
    scenario = Scenario(endpoint="db", neighbor="client", neighbor_endpoint="db")
    assert (
        run_scenarios(charm("target", EndpointType.PROVIDES), [scenario], Config(), "k8s", "kubernetes", tmp_path) == 1
    )


@pytest.mark.parametrize("xml", ["<testsuites/>", "<invalid", "<testsuite><testcase/></testsuite>"])
def test_invalid_junit_fails_without_suppressing_later_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xml: str
) -> None:
    calls = 0

    def execute(argv: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        report = Path(argv[argv.index("--junitxml") + 1])
        report.write_text(xml if calls == 1 else '<testsuite><testcase name="test_deploy"/></testsuite>')
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("ci_runner.runner.subprocess.run", execute)
    scenarios = [Scenario(endpoint="db", neighbor=f"client-{name}", neighbor_endpoint="db") for name in ("a", "b")]
    status = run_scenarios(charm("target", EndpointType.PROVIDES), scenarios, Config(), "k8s", "kubernetes", tmp_path)
    assert status == 1
    assert calls == 2


def test_reporting_requires_credentials_before_testing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_OBSERVER_API", raising=False)
    monkeypatch.delenv("TEST_OBSERVER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="requires both"):
        run_scenarios(
            charm("target", EndpointType.PROVIDES), [], Config(observer_execution_id=1), "k8s", "kubernetes", tmp_path
        )


def test_junit_results_match_observer_contract(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase name="pass" classname="tests.deploy"><system-out>hello</system-out></testcase>'
        '<testcase name="fail"><failure message="bad"/></testcase>'
        '<testcase name="error"><error message="setup"/></testcase>'
        '<testcase name="skip"><skipped message="unsupported"/></testcase>'
        "</testsuite></testsuites>"
    )
    results = junit_results(report, "scenario-0")
    assert [result.status for result in results] == ["PASSED", "FAILED", "FAILED", "SKIPPED"]
    assert results[0].category == "scenario-0/pytest"
    assert results[0].template_id == "tests.deploy"
    assert results[0].io_log == "hello"
    assert results[2].comment == "setup"


def test_empty_report_is_not_success(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text("<testsuites/>")
    with pytest.raises(ValueError, match="No test results"):
        junit_results(report, "scenario-0")


def test_observer_names_are_unique_across_scenarios_and_test_classes(tmp_path: Path) -> None:
    # GIVEN Test Observer identifies test cases by name, not category
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite name="pytest">'
        '<testcase name="test_deploy" classname="tests.first"/>'
        '<testcase name="test_deploy" classname="tests.second"/>'
        "</testsuite>"
    )

    # WHEN the same tests run against two neighbors in one execution
    results = junit_results(report, "db-client-a") + junit_results(report, "db-client-b")

    # THEN every result has a distinct Test Observer identity
    assert len({result.name for result in results}) == 4
    assert results[0].name == "db-client-a/tests.first::test_deploy"


@pytest.mark.parametrize(
    "xml",
    ["<testsuite><testcase/></testsuite>", '<testsuite><testcase name=""/></testsuite>'],
    ids=["missing-name", "empty-name"],
)
def test_unnamed_junit_result_is_invalid(tmp_path: Path, xml: str) -> None:
    report = tmp_path / "results.xml"
    report.write_text(xml)
    with pytest.raises(ValueError, match="testcase name"):
        junit_results(report, "scenario-0")


def test_real_cli_plan_uses_local_archive_without_a_published_target(tmp_path: Path) -> None:
    artifact = tmp_path / "unpublished target.charm"
    with ZipFile(artifact, "w") as archive:
        archive.writestr(
            "metadata.yaml",
            "name: unpublished-local-db\nprovides:\n  database:\n    interface: db\n",
        )
        archive.writestr("manifest.yaml", 'bases:\n- name: ubuntu\n  channel: "24.04"\n  architectures: [amd64]\n')
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "arch": "amd64",
                "scenarios": [{"endpoint": "database", "neighbor": "client", "neighbor_endpoint": "db"}],
            }
        )
    )
    output = tmp_path / "results"
    argv = [
        sys.executable,
        "-m",
        "ci_runner",
        "--charm-path",
        str(artifact),
        "--config",
        str(config),
        "--cloud",
        "not-contacted",
        "--platform",
        "machine",
        "--output-dir",
        str(output),
        "--plan",
    ]
    completed = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((output / "scenarios.json").read_text()) == [
        {"endpoint": "database", "neighbor": "client", "neighbor_endpoint": "db", "pytest_args": []}
    ]
    assert not (output / "scenario-0000").exists()
    repeated = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert "already contains a run" in repeated.stderr


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_cli_rejects_invalid_observer_execution_id(tmp_path: Path, value: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ci_runner",
            "--charm-path",
            str(tmp_path / "absent.charm"),
            "--cloud",
            "localhost",
            "--platform",
            "machine",
            "--output-dir",
            str(tmp_path / "results"),
            "--observer-execution-id",
            value,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "--observer-execution-id" in completed.stderr
