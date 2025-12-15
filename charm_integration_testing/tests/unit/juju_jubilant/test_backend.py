# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta
from typing import Any, Callable

import jubilant
import pytest
import yaml
from juju import JujuWaitState, JujuWaitTimeoutError
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient
from pydantic.dataclasses import dataclass


class JubilantClientStub:
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

    def status(self) -> jubilant.Status:
        self.call_count += 1
        return jubilant.Status(
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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

            # Track call count
            call_count = 0

            def ready_func(status: jubilant.Status) -> tuple[bool, JujuWaitState]:
                nonlocal call_count
                call_count += 1
                # Always ready but timeout before getting 100 successes
                return (True, JujuWaitState(message="always ready"))

            # WHEN wait times out while always being ready
            with pytest.raises(JujuWaitTimeoutError) as exc_info:
                backend.wait(
                    "test-model",
                    ready=ready_func,
                    timeout=timedelta(milliseconds=100),
                    successes=100,  # Need 100 successes but will timeout
                    delay=timedelta(milliseconds=10),
                )

            # THEN insufficient_status_checks is set
            assert exc_info.value.wait_state.insufficient_status_checks
            assert call_count < 100

        def test_wait_with_error_callback(self) -> None:
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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

    class TestWaitIdle:
        def test_wait_idle(self) -> None:
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            backend = JubilantBackend(client)  # type: ignore[arg-type]

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
            secret_id = JubilantBackend(client).add_secret("test-model", "my-secret", {"key": "value"})  # type: ignore[arg-type]

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
            content = JubilantBackend(client).read_secret("test-model", "my-secret")  # type: ignore[arg-type]

            # THEN
            assert content == {"my-key": "my-value"}

    class TestGrantSecret:
        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=JubilantCliStub())

            # WHEN
            JubilantBackend(client).grant_secret("test-model", "my-secret", "my-application")  # type: ignore[arg-type]

            # THEN
            assert ("grant-secret", "my-secret", "my-application") in client.client.executions

    class TestRemoveSecret:
        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=JubilantCliStub())

            # WHEN
            JubilantBackend(client).remove_secret("test-model", "my-secret")  # type: ignore[arg-type]

            # THEN
            assert ("remove-secret", "my-secret") in client.client.executions

    class TestDeployApplication:
        @dataclass
        class DeployStub:
            charm: str | None = None
            app: str | None = None

            def deploy(self, charm: str, app: str | None = None) -> None:
                self.charm = charm
                self.app = app

        def test(self) -> None:
            # GIVEN
            stub = self.DeployStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).deploy_application("test-model", charm="my-charm", application="my-app")  # type: ignore[arg-type]

            # THEN
            assert stub.charm == "my-charm"
            assert stub.app == "my-app"

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
            JubilantBackend(client).configure_application("test-model", "my-app", {"k": "v"})  # type: ignore[arg-type]

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
            JubilantBackend(client).scp("test-model", source="a", destination="b")  # type: ignore[arg-type]

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
            JubilantBackend(client).ssh("test-model", application="my-app", command="ls -l")  # type: ignore[arg-type]

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
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/0")  # type: ignore[arg-type]

            # THEN
            assert ip == "10.0.0.1"

        def test_by_leader(self) -> None:
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/leader")  # type: ignore[arg-type]

            # THEN
            assert ip == "10.0.0.2"

        def test_not_found(self) -> None:
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN / THEN
            try:
                JubilantBackend(client).unit_ip("test-model", "my-app/99")  # type: ignore[arg-type]
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
            charm_revisions = JubilantBackend(client).get_charm_revisions("test-model")  # type: ignore[arg-type]

            # THEN
            assert charm_revisions == {("my-charm", 1)}
