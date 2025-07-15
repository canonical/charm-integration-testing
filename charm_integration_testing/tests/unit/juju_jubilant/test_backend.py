# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta

import yaml
from jubilant import SecretURI
from juju import JujuWaitTimeoutError
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


class TestJubilantBackend:
    class TestWaitIdle:
        @dataclass
        class WaitStub:
            raise_exception: bool = False
            timeout: float | None = None
            successes: int | None = None

            def wait(self, ready, error, timeout, successes, delay):
                self.timeout = timeout
                self.successes = successes
                if self.raise_exception:
                    raise TimeoutError

        def test_success(self):
            # GIVEN a 20 second timeout
            timeout = timedelta(seconds=20)
            # AND a 5 second period
            period = timedelta(seconds=5)
            # AND a jubilant client
            client = JubilantClientStub(client=self.WaitStub())

            # WHEN the backend is called with the args
            JubilantBackend(client).wait_idle("model", timeout=timeout, period=period)

            # THEN the timeout was provided
            assert client.client.timeout == 20
            # AND the period was rounded to the number of seconds
            assert client.client.successes == 5

        def test_timeout(self):
            # GIVEN a jubilant client that will timeout when waiting
            client = JubilantClientStub(client=self.WaitStub(raise_exception=True))

            # WHEN the backend is called
            try:
                JubilantBackend(client).wait_idle("model", timeout=None, period=None)
            except JujuWaitTimeoutError:
                raised = True
            else:
                raised = False

            # THEN the timeout was raised
            assert raised

    class TestAddSecret:
        @dataclass
        class AddSecretStub:
            secrets: dict = field(default_factory=dict)
            secret_uri: str = "secret:test-secret-id"

            def add_secret(self, name: str, content: dict[str, str]):
                self.secrets[name] = content
                return SecretURI(self.secret_uri)

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
