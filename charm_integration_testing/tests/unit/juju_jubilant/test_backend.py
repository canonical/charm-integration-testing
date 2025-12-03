# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta
from typing import Any

import jubilant
import yaml
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
class WaitStub:
    raise_exception: bool = False

    def wait(self, ready: Any, timeout: float, delay: float, **kwargs: Any) -> None:
        self.ready = ready
        self.timeout = timeout
        self.delay = delay
        self.kwargs = kwargs

        if self.raise_exception:
            raise TimeoutError


class TestJubilantBackend:
    class TestWaitIdle:
        def test_wait_idle(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_idle("test-model", timedelta(seconds=10), timedelta(seconds=5))

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_idle("test-model", timedelta(seconds=10), timedelta(seconds=5))
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitApplicationSettled:
        def test_application_settled(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_application_settled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_application_settled("test-model", "my-app", timeout=timedelta(seconds=10))
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitApplicationScaled:
        def test_application_scaled(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_application_scaled("test-model", "my-app", timeout=timedelta(seconds=10))

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_application_scaled("test-model", "my-app", timeout=timedelta(seconds=10))
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitForUnitMessage:
        def test_unit_message(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_for_unit_message(
                "test-model", "my-unit", "my-message", timeout=timedelta(seconds=10)
            )

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_for_unit_message(
                    "test-model", "my-unit", "my-message", timeout=timedelta(seconds=10)
                )
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitForRemoval:
        def test_removal(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_for_removal("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_for_removal("test-model", ["my-app"], timeout=timedelta(seconds=10))
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitForRemovalOfIntegration:
        def test_removal_of_integration(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            from juju import JujuIntegrationApplication

            endpoint_1 = JujuIntegrationApplication("app1", "endpoint1")
            endpoint_2 = JujuIntegrationApplication("app2", "endpoint2")
            JubilantBackend(client).wait_for_removal_of_integration(
                "test-model", endpoint_1, endpoint_2, timeout=timedelta(seconds=10)
            )

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            from juju import JujuIntegrationApplication

            endpoint_1 = JujuIntegrationApplication("app1", "endpoint1")
            endpoint_2 = JujuIntegrationApplication("app2", "endpoint2")
            try:
                JubilantBackend(client).wait_for_removal_of_integration(
                    "test-model", endpoint_1, endpoint_2, timeout=timedelta(seconds=10)
                )
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestWaitForRemovalOfUnits:
        def test_removal_of_units(self) -> None:
            # GIVEN
            stub = WaitStub()
            client = JubilantClientStub(client=stub)

            # WHEN
            JubilantBackend(client).wait_for_removal_of_units("test-model", ["my-app"], timeout=timedelta(seconds=10))

            # THEN the timeout and delay are set correctly
            assert stub.timeout == 10
            assert stub.delay == 1

        def test_timeout(self) -> None:
            # GIVEN
            stub = WaitStub(raise_exception=True)
            client = JubilantClientStub(client=stub)

            # WHEN
            try:
                JubilantBackend(client).wait_for_removal_of_units(
                    "test-model", ["my-app"], timeout=timedelta(seconds=10)
                )
            except TimeoutError:
                # THEN
                pass
            else:
                # AND
                assert False

    class TestAddSecret:
        @dataclass
        class AddSecretStub:
            secrets: dict = field(default_factory=dict)
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

            def deploy(self, charm: str, app: str | None = None) -> None:
                self.charm = charm
                self.app = app

        def test(self) -> None:
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
            def __init__(self, units: list["TestJubilantBackend.TestUnitIp.Unit"]) -> None:
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
