# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta
from typing import Any, Callable, Mapping

import jubilant
import pytest
import yaml
from juju import JujuWaitState, JujuWaitTimeoutError
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient
from pydantic.dataclasses import dataclass


class JubilantClientStub(JubilantClient):
    client: Any

    def __init__(self, client: Any) -> None:
        self.client = client

    def model(self, model: str) -> Any:
        return self.client


@dataclass
class JubilantCliStub:
    results: dict[tuple[str, ...], str] = field(default_factory=dict)
    executions: list[tuple[str, ...]] = field(default_factory=list)

    def cli(self, *args: str) -> str:
        self.executions.append(tuple(args))
        return self.results.get(tuple(args), "")


class TestJubilantClient:
    def test_model(self) -> None:
        # GIVEN a jubilant client
        client = JubilantClient()

        # WHEN a model is requested
        model = client.model("my-model")

        # THEN the jubilant.Juju has the model
        assert model.model == "my-model"


@dataclass
class StatusStub:
    """Stub for status() method"""

    call_count: int = 0
    application_statuses: dict[str, str] = field(default_factory=dict)
    unit_workload_statuses: dict[str, str] = field(default_factory=dict)
    unit_juju_statuses: dict[str, str] = field(default_factory=dict)

    def status(self) -> jubilant.Status:
        self.call_count += 1
        status = jubilant.Status(
            model=jubilant.statustypes.ModelStatus(
                name="test-model",
                type="caas",
                controller="test",
                cloud="test",
                version="3.0.0",
            ),
            machines={},
            apps={},
        )

        # If statuses are provided, populate the apps dictionary for any apps/units supplied.

        # 1. Collect all application and unit names from application statuses and unit statuses.
        app_names = set(self.application_statuses.keys())
        unit_derived_app_names = set()
        for status_dict in (self.unit_workload_statuses, self.unit_juju_statuses):
            for unit_id in status_dict.keys():
                app_name = unit_id.split("/")[0]
                unit_derived_app_names.add(app_name)
        app_names.update(unit_derived_app_names)

        units_by_app_name = {}
        for status_dict in (self.unit_workload_statuses, self.unit_juju_statuses):
            for unit_id in status_dict.keys():
                units_by_app_name.setdefault(unit_id.split("/")[0], set()).add(unit_id)

        # 2. For each application, create AppStatus with relevant UnitStatus entries.
        for app in app_names:
            unit_names = units_by_app_name.get(app, set())
            units = {
                unit_name: jubilant.statustypes.UnitStatus(
                    workload_status=self.unit_workload_statuses.get(unit_name, "unknown"),
                    juju_status=self.unit_juju_statuses.get(unit_name, "unknown"),
                )
                for unit_name in unit_names
            }
            status.apps[app] = jubilant.statustypes.AppStatus(
                # Required - just supplying some dummy values
                charm="test-charm",
                charm_origin="charmhub",
                charm_name="test-charm",
                charm_rev=1,
                exposed=False,
                # These are the parameters we actually care about:
                app_status=jubilant.statustypes.StatusInfo(current=self.application_statuses.get(app, "unknown")),
                units=units,
            )

        return status


@dataclass
class WaitStub:
    """Stub for wait() method that can be configured to succeed or fail"""

    raise_timeout: bool = False
    call_count: int = 0

    def wait(
        self,
        model: str,
        ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
        error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None,
        timeout: timedelta | None = None,
        successes: int | None = None,
        delay: timedelta | None = None,
        strict_timeout: bool = False,
    ) -> None:
        self.call_count += 1
        if self.raise_timeout:
            raise JujuWaitTimeoutError()


class TestJubilantBackend:
    class TestWait:
        def test_wait_success(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait is called with a ready condition that is immediately true
            backend.wait(
                "test-model",
                ready=lambda status: (True, JujuWaitState(message="ready")),
                timeout=timedelta(seconds=10),
                successes=3,
                delay=timedelta(milliseconds=10),
            )

            # THEN status was called 3 times (for 3 successes)
            assert stub.call_count == 3

        def test_wait_timeout_with_failures(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait is called with a ready condition that is never true
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=lambda status: (False, JujuWaitState(message="not ready")),
                    timeout=timedelta(milliseconds=100),
                    successes=3,
                    delay=timedelta(milliseconds=10),
                )

            # THEN the wait state has the correct message
            assert exc_info.value.wait_state.message == "not ready"
            assert not exc_info.value.wait_state.insufficient_status_checks

        def test_wait_timeout_insufficient_checks(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                call_count += 1
                # Always ready but timeout before getting 100 successes
                return (True, JujuWaitState(message="always ready"))

            # WHEN wait times out while always being ready with strict_timeout=True
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=ready_func,
                    timeout=timedelta(milliseconds=100),
                    successes=100,  # Need 100 successes but will timeout
                    delay=timedelta(milliseconds=10),
                    strict_timeout=True,  # Enforce timeout even when making progress
                )

            # THEN insufficient_status_checks is set
            assert exc_info.value.wait_state.insufficient_status_checks
            assert call_count < 100

        def test_wait_with_error_callback(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait is called with an error condition that triggers
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=lambda status: (False, JujuWaitState(message="not ready")),
                    error=lambda status: (True, JujuWaitState(message="error occurred")),
                    timeout=timedelta(seconds=10),
                    successes=3,
                    delay=timedelta(milliseconds=10),
                )

            # THEN the error message is in the wait state
            assert exc_info.value.wait_state.message == "error occurred"

        def test_wait_success_resets_on_failure(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track ready states: ready, ready, not ready, ready, ready, ready
            ready_states = [True, True, False, True, True, True]
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                is_ready = ready_states[min(call_count, len(ready_states) - 1)]
                call_count += 1
                return (is_ready, JujuWaitState(message="test"))

            # WHEN wait is called needing 3 successes
            backend.wait(
                "test-model",
                ready=ready_func,
                timeout=timedelta(seconds=10),
                successes=3,
                delay=timedelta(milliseconds=10),
            )

            # THEN we needed 6 calls (2 ready, 1 fail resets count, 3 ready to succeed)
            assert call_count == 6

        def test_wait_extends_timeout_when_making_progress(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count - we need 10 successes but timeout after 5 checks
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                call_count += 1
                # Always ready
                return (True, JujuWaitState(message="ready"))

            # WHEN wait is called with strict_timeout=False (default) and making progress
            backend.wait(
                "test-model",
                ready=ready_func,
                timeout=timedelta(milliseconds=50),  # Very short timeout
                successes=10,  # Need 10 successes
                delay=timedelta(milliseconds=10),
                strict_timeout=False,
            )

            # THEN we got all 10 successes despite timeout being exceeded
            assert call_count == 10

        def test_wait_enforces_strict_timeout(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                call_count += 1
                # Always ready
                return (True, JujuWaitState(message="ready"))

            # WHEN wait is called with strict_timeout=True
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=ready_func,
                    timeout=timedelta(milliseconds=50),  # Very short timeout
                    successes=100,  # Need 100 successes (impossible in time)
                    delay=timedelta(milliseconds=10),
                    strict_timeout=True,
                )

            # THEN timeout was enforced even though making progress
            assert call_count < 100
            assert exc_info.value.wait_state.insufficient_status_checks

        def test_wait_timeout_when_not_making_progress(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                call_count += 1
                # Never ready
                return (False, JujuWaitState(message="not ready"))

            # WHEN wait is called with strict_timeout=False but not making progress
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=ready_func,
                    timeout=timedelta(milliseconds=50),
                    successes=10,
                    delay=timedelta(milliseconds=10),
                    strict_timeout=False,
                )

            # THEN timeout was enforced because success_count stayed at 0
            assert exc_info.value.wait_state.message == "not ready"
            assert not exc_info.value.wait_state.insufficient_status_checks

    class TestWaitIdle:
        def test_wait_idle(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN
            backend.wait_idle("test-model", timedelta(seconds=10), count=3)

            # THEN status was called 3 times
            assert stub.call_count == 3

        def test_timeout(self) -> None:
            # GIVEN a backend with stubbed wait that raises timeout
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_idle("test-model", timedelta(milliseconds=100), count=5)

        def test_wait_idle_with_strict_timeout(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait_idle is called with strict_timeout=True
            backend.wait_idle("test-model", timedelta(seconds=10), count=3, strict_timeout=True)

            # THEN status was called 3 times
            assert stub.call_count == 3

        def test_wait_idle_extends_timeout_by_default(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait_idle is called (strict_timeout defaults to False)
            backend.wait_idle("test-model", timedelta(milliseconds=50), count=10)

            # THEN it completed all 10 checks despite short timeout
            assert stub.call_count == 10

        def test_wait_idle_on_subset_of_model(self) -> None:
            # GIVEN
            stub = StatusStub(application_statuses={"app1": "active", "app2": "active", "app3": "blocked"})
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait_idle is called with specific applications/units
            backend.wait_idle(
                "test-model",
                timedelta(seconds=3),
                count=3,
                applications=["app1", "app2"],
            )

            # THEN status was called 3 times
            assert stub.call_count == 3

    class TestWaitApplicationSettled:
        def test_application_settled(self) -> None:
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_application_settled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_application_settled("test-model", "my-app", timeout=timedelta(milliseconds=100))

    class TestWaitApplicationScaled:
        def test_application_scaled(self) -> None:
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_application_scaled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_application_scaled("test-model", "my-app", timeout=timedelta(milliseconds=100))

    class TestWaitForUnitMessage:
        def test_unit_message(self) -> None:
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_for_unit_message("test-model", "my-app/0", "my-message", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_unit_message(
                    "test-model", "my-unit", "my-message", timeout=timedelta(milliseconds=100)
                )

    class TestWaitForRemoval:
        def test_removal(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN
            backend.wait_for_removal("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN status was called
            assert stub.call_count >= 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_removal("test-model", ["my-app"], timeout=timedelta(milliseconds=100))

    class TestWaitForRemovalOfIntegration:
        def test_removal_of_integration(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN
            from juju import JujuIntegrationApplication

            endpoint_1 = JujuIntegrationApplication("app1", "endpoint1")
            endpoint_2 = JujuIntegrationApplication("app2", "endpoint2")
            backend.wait_for_removal_of_integration("test-model", endpoint_1, endpoint_2, timeout=timedelta(seconds=10))

            # THEN status was called
            assert stub.call_count >= 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            from juju import JujuIntegrationApplication

            endpoint_1 = JujuIntegrationApplication("app1", "endpoint1")
            endpoint_2 = JujuIntegrationApplication("app2", "endpoint2")
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_removal_of_integration(
                    "test-model", endpoint_1, endpoint_2, timeout=timedelta(milliseconds=100)
                )

    class TestWaitForRemovalOfUnits:
        def test_removal_of_units(self) -> None:
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_for_removal_of_units("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self) -> None:
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_removal_of_units("test-model", ["my-app"], timeout=timedelta(milliseconds=100))

    class TestAddSecret:
        @dataclass
        class AddSecretStub:
            secrets: dict[str, dict[str, str]] = field(default_factory=dict)
            secret_uri: str = "secret:test-secret-id"

            def add_secret(self, name: str, content: dict[str, str]) -> jubilant.SecretURI:
                self.secrets[name] = content
                return jubilant.SecretURI(self.secret_uri)

        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=self.AddSecretStub())

            # WHEN
            secret_id = JubilantBackend(client).add_secret("test-model", "my-secret", {"key": "value"})

            # THEN
            assert secret_id == "test-secret-id"
            # AND
            assert client.client.secrets["my-secret"] == {"key": "value"}

    class TestReadSecret:
        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(
                client=JubilantCliStub(
                    results={
                        (
                            "show-secret",
                            "my-secret",
                            "--reveal",
                            "--format=yaml",
                        ): yaml.dump(
                            {
                                "my-secret": {
                                    "content": {
                                        "my-key": "my-value",
                                    }
                                }
                            }
                        )
                    }
                )
            )

            # WHEN
            content = JubilantBackend(client).read_secret("test-model", "my-secret")

            # THEN
            assert content == {"my-key": "my-value"}

    class TestGrantSecret:
        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=JubilantCliStub())

            # WHEN
            JubilantBackend(client).grant_secret("test-model", "my-secret", "my-application")

            # THEN
            assert ("grant-secret", "my-secret", "my-application") in client.client.executions

    class TestRemoveSecret:
        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=JubilantCliStub())

            # WHEN
            JubilantBackend(client).remove_secret("test-model", "my-secret")

            # THEN
            assert ("remove-secret", "my-secret") in client.client.executions

    class TestDeployApplication:
        @dataclass
        class DeployStub:
            charm: str | None = None
            app: str | None = None

            def deploy(self, charm: str, app: str | None = None, config: Any = None) -> None:
                self.charm = charm
                self.app = app
                self.config = config

        def test(self) -> None:
            # GIVEN
            stub = self.DeployStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).deploy_application(
                "test-model", charm="my-charm", application="my-app", config={"setting": "value"}
            )

            # THEN
            assert stub.charm == "my-charm"
            assert stub.app == "my-app"
            assert stub.config == {"setting": "value"}

    class TestConfigureApplication:
        @dataclass
        class ConfigStub:
            app: str | None = None
            values: dict[str, str] = field(default_factory=dict)

            def config(self, app: str, values: dict[str, str]) -> None:
                self.app = app
                self.values = values

        def test(self) -> None:
            # GIVEN
            stub = self.ConfigStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).configure_application("test-model", "my-app", {"k": "v"})

            # THEN
            assert stub.app == "my-app"
            assert stub.values == {"k": "v"}

    class TestScp:
        @dataclass
        class ScpStub:
            source: str = ""
            destination: str = ""

            def scp(self, source: str, destination: str) -> None:
                self.source = source
                self.destination = destination

        def test(self) -> None:
            # GIVEN
            stub = self.ScpStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).scp("test-model", source="a", destination="b")

            # THEN
            assert stub.source == "a"
            assert stub.destination == "b"

    class TestSsh:
        @dataclass
        class SshStub:
            target: str = ""
            command: str = ""

            def ssh(self, target: str, command: str) -> None:
                self.target = target
                self.command = command

        def test(self) -> None:
            # GIVEN
            stub = self.SshStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).ssh("test-model", application="my-app", command="ls -l")

            # THEN
            assert stub.target == "my-app"
            assert stub.command == "ls -l"

    class TestUnitIp:
        class Unit:
            def __init__(self, address: str, leader: bool = False) -> None:
                self.address = address
                self.leader = leader

        class AppStatus:
            def __init__(self, units: dict[str, "TestJubilantBackend.TestUnitIp.Unit"]) -> None:
                self.units = units

        class ModelStatus:
            def __init__(self) -> None:
                self.apps = {
                    "my-app": TestJubilantBackend.TestUnitIp.AppStatus(
                        {
                            "my-app/0": TestJubilantBackend.TestUnitIp.Unit("10.0.0.1"),
                            "my-app/1": TestJubilantBackend.TestUnitIp.Unit("10.0.0.2", leader=True),
                        }
                    )
                }

        class StatusStub:
            def status(self) -> "TestJubilantBackend.TestUnitIp.ModelStatus":
                return TestJubilantBackend.TestUnitIp.ModelStatus()

        def test_by_unit_id(self) -> None:
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/0")

            # THEN
            assert ip == "10.0.0.1"

        def test_by_leader(self) -> None:
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/leader")

            # THEN
            assert ip == "10.0.0.2"

        def test_not_found(self) -> None:
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN / THEN
            try:
                JubilantBackend(client).unit_ip("test-model", "my-app/99")
            except KeyError as e:
                assert "my-app/99" in str(e)
            else:
                assert False, "Expected KeyError"

    class TestGetCharmRevisions:
        class StatusStub:
            def __init__(self, charm: str, charm_rev: int) -> None:
                self.apps = {
                    "my-app": jubilant.statustypes.AppStatus(
                        charm=charm,
                        charm_origin="charmhub",
                        charm_name=charm,
                        charm_rev=charm_rev,
                        exposed=True,
                    )
                }

        class ModelStatus:
            def __init__(self, charm: str, charm_rev: int) -> None:
                self.apps = TestJubilantBackend.TestGetCharmRevisions.StatusStub(charm, charm_rev).apps

        class StatusStubClient:
            def status(self) -> "TestJubilantBackend.TestGetCharmRevisions.ModelStatus":
                return TestJubilantBackend.TestGetCharmRevisions.ModelStatus("my-charm", 1)

        class ModelStub:
            client: "TestJubilantBackend.TestGetCharmRevisions.StatusStubClient"

            def __init__(self, client: "TestJubilantBackend.TestGetCharmRevisions.StatusStubClient") -> None:
                self.client = client

            def status(self) -> "TestJubilantBackend.TestGetCharmRevisions.ModelStatus":
                return self.client.status()

        def test_get_charm_revisions(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=self.ModelStub(client=self.StatusStubClient()))

            # WHEN
            charm_revisions = JubilantBackend(client).get_charm_revisions("test-model")

            # THEN
            assert charm_revisions == {("my-charm", 1)}

    class TestRunAction:
        @dataclass
        class ActionStub:
            unit: str = ""
            action: str = ""
            params: dict[str, Any] = field(default_factory=dict)

            def run(self, unit: str, action: str, params: Mapping[str, Any]) -> jubilant.Task:
                self.unit = unit
                self.action = action
                self.params = params
                return jubilant.Task(
                    id="123", status="failed", return_code=1, message="error", results={"output": "error output"}
                )

        def test(self) -> None:
            # This test does two things:
            # * Verifies basic plumbing of run_action via our backend class.
            # * Verifies that we correctly transform from jubilant.Task to our own JujuTask.

            # GIVEN
            stub = self.ActionStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            task = JubilantBackend(client).run_action(
                "test-model", unit="my-app/0", action="restart-service", params={"force": True}
            )

            # THEN
            assert stub.unit == "my-app/0"
            assert stub.action == "restart-service"
            assert stub.params == {"force": True}

            # The following just verifies that we transform from the jubilant.Task to our own JujuTask as expected.
            assert task.id == "123"
            assert task.return_code == 1
            assert task.status == "failed"
            assert task.message == "error"
            assert task.output == "error output"

    class TestGetApplicationConfig:
        @dataclass
        class ConfigStub:
            app: str = ""

            def config(self, app: str) -> dict[str, Any]:
                self.app = app
                return {"setting1": "value1", "setting2": "value2"}

        def test(self) -> None:
            # GIVEN
            stub = self.ConfigStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            config = JubilantBackend(client).get_application_config("test-model", "my-app")

            # THEN
            assert stub.app == "my-app"
            assert config == {"setting1": "value1", "setting2": "value2"}
