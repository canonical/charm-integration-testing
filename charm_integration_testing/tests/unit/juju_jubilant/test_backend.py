# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import timedelta

from juju import JujuWaitTimeoutError
from juju_jubilant.backend import JubilantBackend, JubilantClient
from pydantic.dataclasses import dataclass


@dataclass
class JubilantJujuStub:
    wait_raise_exception: bool = False
    wait_timeout: float | None = None
    wait_successes: int | None = None

    def wait(self, ready, error, timeout, successes, delay):
        self.wait_timeout = timeout
        self.wait_successes = successes
        if self.wait_raise_exception:
            raise TimeoutError


@dataclass
class JubilantClientStub:
    model_client: JubilantJujuStub = field(default_factory=JubilantJujuStub)

    def model(self, model: str) -> JubilantJujuStub:
        return self.model_client


class TestJubilantClient:
    def test_model(self):
        # GIVEN a jubilant client
        client = JubilantClient()

        # WHEN a model is requested
        model = client.model("my-model")

        # THEN the jubilant.Juju has the model
        assert model.model == "my-model"


class TestJubilantBackend:
    def test_wait_idle_success(self):
        # GIVEN a 20 second timeout
        timeout = timedelta(seconds=20)
        # AND a 5 second period
        period = timedelta(seconds=5)
        # AND a jubilant client
        client = JubilantClientStub()

        # WHEN the backend is called with the args
        JubilantBackend(client).wait_idle("model", timeout=timeout, period=period)

        # THEN the timeout was provided
        assert client.model_client.wait_timeout == 20
        # AND the period was rounded to the number of seconds
        assert client.model_client.wait_successes == 5

    def test_wait_idle_timeout(self):
        # GIVEN a jubilant client that will timeout when waiting
        client = JubilantClientStub(model_client=JubilantJujuStub(wait_raise_exception=True))

        # WHEN the backend is called
        try:
            JubilantBackend(client).wait_idle("model", timeout=None, period=None)
        except JujuWaitTimeoutError:
            raised = True
        else:
            raised = False

        # THEN the timeout was raised
        assert raised
