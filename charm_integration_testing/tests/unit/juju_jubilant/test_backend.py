# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta

import jubilant
import pytest
import yaml
from juju import JujuWaitState, JujuWaitTimeoutError
from juju_jubilant.backend import JubilantBackend, JubilantClient
from pydantic.dataclasses import dataclass


class JubilantClientStub:
    client: any

    def __init__(self, client: any):
        self.client = client

    def model(self, model: str) -> any:
        return self.client


@dataclass
class JubilantCliStub:
    results: dict[tuple[str, ...], str] = field(default_factory=dict)
    executions: list[tuple[str, ...]] = field(default_factory=list)

    def cli(self, *args: str) -> str:
        self.executions.append(tuple(args))
        return self.results.get(tuple(args), "")


class TestJubilantClient:
    def test_model(self):
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

    def status(self):
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
        ready,
        error=None,
        timeout=None,
        successes=None,
        delay=None,
        strict_timeout=False,
    ):
        self.call_count += 1
        if self.raise_timeout:
            raise JujuWaitTimeoutError()


class TestJubilantBackend:
    class TestWait:
        def test_wait_success(self):
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

        def test_wait_timeout_with_failures(self):
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

        def test_wait_timeout_insufficient_checks(self):
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status):
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

        def test_wait_with_error_callback(self):
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

        def test_wait_success_resets_on_failure(self):
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track ready states: ready, ready, not ready, ready, ready, ready
            ready_states = [True, True, False, True, True, True]
            call_count = 0

            def ready_func(status):
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

        def test_wait_extends_timeout_when_making_progress(self):
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count - we need 10 successes but timeout after 5 checks
            call_count = 0

            def ready_func(status):
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

        def test_wait_enforces_strict_timeout(self):
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status):
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

        def test_wait_timeout_when_not_making_progress(self):
            # GIVEN a backend with mocked status
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Track call count
            call_count = 0

            def ready_func(status):
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
        def test_wait_idle(self):
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN
            backend.wait_idle("test-model", timedelta(seconds=10), count=3)

            # THEN status was called 3 times
            assert stub.call_count == 3

        def test_timeout(self):
            # GIVEN a backend with stubbed wait that raises timeout
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_idle("test-model", timedelta(milliseconds=100), count=5)

        def test_wait_idle_with_strict_timeout(self):
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait_idle is called with strict_timeout=True
            backend.wait_idle("test-model", timedelta(seconds=10), count=3, strict_timeout=True)

            # THEN status was called 3 times
            assert stub.call_count == 3

        def test_wait_idle_extends_timeout_by_default(self):
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN wait_idle is called (strict_timeout defaults to False)
            backend.wait_idle("test-model", timedelta(milliseconds=50), count=10)

            # THEN it completed all 10 checks despite short timeout
            assert stub.call_count == 10

    class TestWaitApplicationSettled:
        def test_application_settled(self):
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_application_settled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self):
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_application_settled("test-model", "my-app", timeout=timedelta(milliseconds=100))

    class TestWaitApplicationScaled:
        def test_application_scaled(self):
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_application_scaled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self):
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_application_scaled("test-model", "my-app", timeout=timedelta(milliseconds=100))

    class TestWaitForUnitMessage:
        def test_unit_message(self):
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_for_unit_message("test-model", "my-app/0", "my-message", timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self):
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
        def test_removal(self):
            # GIVEN
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # WHEN
            backend.wait_for_removal("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN status was called
            assert stub.call_count >= 1

        def test_timeout(self):
            # GIVEN
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN / THEN
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_removal("test-model", ["my-app"], timeout=timedelta(milliseconds=100))

    class TestWaitForRemovalOfIntegration:
        def test_removal_of_integration(self):
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

        def test_timeout(self):
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
        def test_removal_of_units(self):
            # GIVEN
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN
            backend.wait_for_removal_of_units("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN wait was called
            assert wait_stub.call_count == 1

        def test_timeout(self):
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
            secrets: dict = field(default_factory=dict)
            secret_uri: str = "secret:test-secret-id"

            def add_secret(self, name: str, content: dict[str, str]):
                self.secrets[name] = content
                return jubilant.SecretURI(self.secret_uri)

        def test(self):
            # GIVEN
            client = JubilantClientStub(client=self.AddSecretStub())

            # WHEN
            secret_id = JubilantBackend(client).add_secret("test-model", "my-secret", {"key": "value"})

            # THEN
            assert secret_id == "test-secret-id"
            # AND
            assert client.client.secrets["my-secret"] == {"key": "value"}

    class TestReadSecret:
        def test(self):
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
        def test(self):
            # GIVEN
            client = JubilantClientStub(client=JubilantCliStub())

            # WHEN
            JubilantBackend(client).grant_secret("test-model", "my-secret", "my-application")

            # THEN
            assert ("grant-secret", "my-secret", "my-application") in client.client.executions

    class TestRemoveSecret:
        def test(self):
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

            def deploy(self, charm: str, app: str | None = None):
                self.charm = charm
                self.app = app

        def test(self):
            # GIVEN
            stub = self.DeployStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).deploy_application("test-model", charm="my-charm", application="my-app")

            # THEN
            assert stub.charm == "my-charm"
            assert stub.app == "my-app"

    class TestConfigureApplication:
        @dataclass
        class ConfigStub:
            app: str | None = None
            values: dict[str, str] = field(default_factory=dict)

            def config(self, app: str, values: dict[str, str]):
                self.app = app
                self.values = values

        def test(self):
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

            def scp(self, source: str, destination: str):
                self.source = source
                self.destination = destination

        def test(self):
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

            def ssh(self, target: str, command: str):
                self.target = target
                self.command = command

        def test(self):
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
            def __init__(self, address, leader=False):
                self.address = address
                self.leader = leader

        class AppStatus:
            def __init__(self, units):
                self.units = units

        class ModelStatus:
            def __init__(self):
                self.apps = {
                    "my-app": TestJubilantBackend.TestUnitIp.AppStatus(
                        {
                            "my-app/0": TestJubilantBackend.TestUnitIp.Unit("10.0.0.1"),
                            "my-app/1": TestJubilantBackend.TestUnitIp.Unit("10.0.0.2", leader=True),
                        }
                    )
                }

        class StatusStub:
            def status(self):
                return TestJubilantBackend.TestUnitIp.ModelStatus()

        def test_by_unit_id(self):
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/0")

            # THEN
            assert ip == "10.0.0.1"

        def test_by_leader(self):
            # GIVEN
            stub = self.StatusStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            ip = JubilantBackend(client).unit_ip("test-model", "my-app/leader")

            # THEN
            assert ip == "10.0.0.2"

        def test_not_found(self):
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
            def __init__(self, charm, charm_rev):
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
            def __init__(self, charm, charm_rev):
                self.apps = TestJubilantBackend.TestGetCharmRevisions.StatusStub(charm, charm_rev).apps

        class StatusStubClient:
            def status(self):
                return TestJubilantBackend.TestGetCharmRevisions.ModelStatus("my-charm", 1)

        class ModelStub:
            client: "TestJubilantBackend.TestGetCharmRevisions.StatusStubClient"

            def __init__(self, client):
                self.client = client

            def status(self):
                return self.client.status()

        def test_get_charm_revisions(self):
            # GIVEN
            client = JubilantClientStub(client=self.ModelStub(client=self.StatusStubClient()))

            # WHEN
            charm_revisions = JubilantBackend(client).get_charm_revisions("test-model")

            # THEN
            assert charm_revisions == {("my-charm", 1)}
