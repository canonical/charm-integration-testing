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
