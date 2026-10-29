# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import sys
import time
from pathlib import Path

import pytest
import test_suite.conftest as suite

pytest_plugins = ["pytester"]


def _client_fixtures(executable: Path) -> str:
    return f"""
import logging
import pytest
from juju import JujuClient, JujuModelHandle
from juju_jubilant import JubilantBackend
from juju_jubilant.client import JubilantClient
from kubernetes_client import KubernetesClientRegistry
from resource_registry import ResourceRegistry

class ExecutableClient(JubilantClient):
    def model(self, model):
        client = super().model(model)
        client.cli_binary = {str(executable)!r}
        return client

@pytest.fixture(scope="session")
def logger():
    return logging.getLogger("timeout-test")

@pytest.fixture(scope="session")
def juju_backend():
    return JubilantBackend(ExecutableClient())

@pytest.fixture
def juju_client(juju_backend, logger):
    return JujuClient(juju_backend, logger)

@pytest.fixture(scope="session")
def kubernetes_client_registry():
    return KubernetesClientRegistry({{}})

@pytest.fixture(scope="session")
def log_dir():
    return None

@pytest.fixture
def require_temp_juju_controller_alive():
    pass

@pytest.fixture
def execution_metadata():
    return lambda *args: None

@pytest.fixture
def record_execution_metadata():
    pass
"""


@pytest.fixture
def sleeping_juju(pytester: pytest.Pytester) -> Path:
    executable = pytester.path / "juju"
    executable.write_text(f"#!{sys.executable} -S\nimport time\ntime.sleep(5)\n")
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize(
    ("fixture_name", "passed", "errors"),
    [
        pytest.param("print_setup_and_teardown_info", 1, 0, id="status-dumps"),
        pytest.param("record_charm_info_execution_metadata", 0, 1, id="charm-metadata"),
        pytest.param("record_juju_execution_metadata", 1, 1, id="version-metadata"),
    ],
)
def test_diagnostics_bound_real_cli_calls(
    pytester: pytest.Pytester,
    sleeping_juju: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    passed: int,
    errors: int,
) -> None:
    # GIVEN the real diagnostic fixtures and an unresponsive Juju executable
    monkeypatch.setattr(suite, "_DIAGNOSTIC_TIMEOUT_SECONDS", 0.2)
    pytester.makeconftest(
        _client_fixtures(sleeping_juju)
        + f"\nfrom test_suite.conftest import {fixture_name}\n"
        + """
@pytest.fixture
def session_resource_registry(logger):
    registry = ResourceRegistry([], logger)
    registry.register(JujuModelHandle(controller="controller", model="model"))
    return registry
"""
    )
    pytester.makepyfile(f"def test_diagnostic(juju_client, {fixture_name}):\n    pass\n")
    start = time.monotonic()

    # WHEN fixture setup and teardown run through pytest
    result = pytester.runpytest("-q", "-o", "log_cli=true", "--log-cli-level=WARNING")

    # THEN diagnostic budgets stop the command, and failures are surfaced
    result.assert_outcomes(passed=passed, errors=errors)
    assert time.monotonic() - start < 3
    if fixture_name == "print_setup_and_teardown_info":
        result.stdout.fnmatch_lines(["*Failed to print status for model*"])
    else:
        result.stdout.fnmatch_lines(["*TimeoutExpired*"])


def test_cleanup_shares_deadline_across_resources(
    pytester: pytest.Pytester, sleeping_juju: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GIVEN two controllers whose destruction blocks, using the real cleanup fixture
    monkeypatch.setattr(suite, "_CLEANUP_TIMEOUT_SECONDS", 0.2)
    pytester.makeconftest(
        _client_fixtures(sleeping_juju) + "\nfrom test_suite.conftest import session_resource_registry\n"
    )
    pytester.makepyfile(
        """
from pathlib import Path
from juju import JujuControllerHandle

def test_register(session_resource_registry, juju_backend):
    def destroy(controller):
        with Path("attempts").open("a") as stream:
            stream.write(controller + "\\n")
        juju_backend.kill_controller(controller)

    for controller in ("first", "second"):
        session_resource_registry.register(
            JujuControllerHandle(controller=controller),
            destroyer=lambda controller=controller: destroy(controller),
        )
"""
    )
    start = time.monotonic()

    # WHEN session teardown runs
    result = pytester.runpytest("-q")

    # THEN all resources are processed under one budget, not a budget per controller
    result.assert_outcomes(passed=1)
    assert time.monotonic() - start < 3
    assert (pytester.path / "attempts").read_text().splitlines() == ["second", "first"]
    result.stdout.fnmatch_lines(["*ResourceTeardownWarning*", "*Operation deadline exceeded*"])
