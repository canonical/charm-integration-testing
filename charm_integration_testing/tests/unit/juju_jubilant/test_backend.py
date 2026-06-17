# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import jubilant
import pytest
import yaml
from juju import CharmChannel, JujuConsumedOfferInfo, JujuIntegrationApplication, JujuWaitState, JujuWaitTimeoutError
from juju.version import JujuVersion
from juju_jubilant.backend import JubilantBackend
from juju_jubilant.client import JubilantClient
from juju_jubilant.wait import _parse_bundle
from pydantic.dataclasses import dataclass

# Shared status outputs for integration tests
STATUS_WITH_SINGLE_INTEGRATION = """Model  Controller  Cloud/Region  Version    SLA          Timestamp
test   microk8s    microk8s      3.1.9      unsupported  12:34:56+00:00

App       Version  Status  Scale  Charm     Channel  Rev  Address        Exposed  Message
database  14       active      1  database  stable    12  10.1.2.3       no       ready
webapp              active      1  webapp    edge      45  10.1.2.4       no       ready

Unit           Workload  Agent  Address   Ports     Message
database/0*    active    idle   10.1.2.3  5432/tcp  ready
webapp/0*      active    idle   10.1.2.4  80/tcp    ready

Integration provider  Requirer         Interface  Type     Message
database:db           webapp:database  pgsql      regular

"""

STATUS_WITH_PEER_INTEGRATION = """Model  Controller  Cloud/Region  Version    SLA          Timestamp
test   microk8s    microk8s      3.1.9      unsupported  12:34:56+00:00

App       Version  Status  Scale  Charm     Channel  Rev  Address        Exposed  Message
database  14       active      3  database  stable    12  10.1.2.3       no       ready

Unit           Workload  Agent  Address   Ports     Message
database/0*    active    idle   10.1.2.3  5432/tcp  ready
database/1     active    idle   10.1.2.5  5432/tcp  ready
database/2     active    idle   10.1.2.6  5432/tcp  ready

Integration provider    Requirer            Interface  Type  Message
database:cluster-peers  database:cluster    cluster    peer

"""

STATUS_WITH_MULTIPLE_INTEGRATIONS = """Model  Controller  Cloud/Region  Version    SLA          Timestamp
test   microk8s    microk8s      3.1.9      unsupported  12:34:56+00:00

App       Version  Status  Scale  Charm     Channel  Rev  Address        Exposed  Message
database  14       active      1  database  stable    12  10.1.2.3       no       ready
webapp              active      1  webapp    edge      45  10.1.2.4       no       ready
cache               active      1  redis     stable     8  10.1.2.5       no       ready

Unit           Workload  Agent  Address   Ports     Message
database/0*    active    idle   10.1.2.3  5432/tcp  ready
webapp/0*      active    idle   10.1.2.4  80/tcp    ready
cache/0*       active    idle   10.1.2.5  6379/tcp  ready

Integration provider  Requirer         Interface  Type     Message
database:db           webapp:database  pgsql      regular
cache:cache           webapp:redis     redis      regular

"""

STATUS_WITH_NO_INTEGRATIONS = """Model  Controller  Cloud/Region  Version    SLA          Timestamp
test   microk8s    microk8s      3.1.9      unsupported  12:34:56+00:00

App       Version  Status  Scale  Charm     Channel  Rev  Address        Exposed  Message
webapp              active      1  webapp    edge      45  10.1.2.4       no       ready

Unit           Workload  Agent  Address   Ports     Message
webapp/0*      active    idle   10.1.2.4  80/tcp    ready

"""

STATUS_EMPTY_MODEL = """Model     Controller  Cloud/Region  Version  SLA          Timestamp
ryan-stg  ryan-stg    ps6-k8s-stg   3.6.13   unsupported  17:41:32Z

Model "admin/ryan-stg" is empty.
"""

STATUS_NO_INTEGRATION_SECTION = """Model       Controller  Cloud/Region  Version  SLA          Timestamp
ryan-stg-2  ryan-stg    ps6-k8s-stg   3.6.13   unsupported  17:43:28Z

App                       Version  Status   Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           waiting      1  self-signed-certificates  1/stable  317  10.152.183.188  no       installing agent

Unit                         Workload  Agent  Address     Ports  Message
self-signed-certificates/0*  running   idle   10.1.2.103
"""

STATUS_WITH_MULTIPLE_INTEGRATIONS_MESSAGE = """Model  Controller  Cloud/Region  Version    SLA          Timestamp
test   microk8s    microk8s      3.1.9      unsupported  12:34:56+00:00

App       Version  Status  Scale  Charm     Channel  Rev  Address        Exposed  Message
database  14       active      1  database  stable    12  10.1.2.3       no       ready
webapp              active      1  webapp    edge      45  10.1.2.4       no       ready
cache               active      1  redis     stable     8  10.1.2.5       no       ready

Unit           Workload  Agent  Address   Ports     Message
database/0*    active    idle   10.1.2.3  5432/tcp  ready
webapp/0*      active    idle   10.1.2.4  80/tcp    ready
cache/0*       active    idle   10.1.2.5  6379/tcp  ready

Integration provider  Requirer         Interface  Type     Message
database:db           webapp:database  pgsql      regular  joining  \

cache:cache           webapp:redis     redis      regular  broken  \

"""  # those extra spaces at the end are intentional and REAL OMG!

# TODO(@motjuste): consider adding integration names with spaces
#   but is that even possible?


class JubilantClientStub(JubilantClient):
    client: Any

    def __init__(self, client: Any) -> None:
        self.client = client

    def model(self, model: str | None) -> Any:
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

        units_by_app_name: dict[str, set[str]] = {}
        for status_dict in (self.unit_workload_statuses, self.unit_juju_statuses):
            for unit_id in status_dict.keys():
                units_by_app_name.setdefault(unit_id.split("/")[0], set()).add(unit_id)

        # 2. For each application, create AppStatus with relevant UnitStatus entries.
        for app in app_names:
            unit_names = units_by_app_name.get(app, set())
            units = {
                unit_name: jubilant.statustypes.UnitStatus(
                    workload_status=jubilant.statustypes.StatusInfo(
                        self.unit_workload_statuses.get(unit_name, "unknown")
                    ),
                    juju_status=jubilant.statustypes.StatusInfo(self.unit_juju_statuses.get(unit_name, "unknown")),
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

    def deploy(self, charm: Any = None, app: str | None = None, config: Any = None, trust: bool = False) -> None:
        pass


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


@dataclass
class ModelExistsStub:
    """Stub for status() used in wait_for_model_to_exist tests.

    Tracks how many times status() is called. When error_stderr is set the stub raises
    jubilant.CLIError with that stderr text; if max_errors is non-zero it stops raising after
    that many calls and succeeds thereafter.
    """

    call_count: int = 0
    error_stderr: str | None = None
    max_errors: int = 0  # 0 means raise on every call while error_stderr is set

    def status(self) -> jubilant.Status:
        self.call_count += 1
        should_error = self.error_stderr is not None and (self.max_errors == 0 or self.call_count <= self.max_errors)
        if should_error:
            raise jubilant.CLIError(1, ["juju", "status"], "", self.error_stderr)
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

    class TestWaitForModelToExist:
        def test_returns_when_status_succeeds(self) -> None:
            # GIVEN a backend whose status() immediately succeeds
            stub = ModelExistsStub()
            backend = JubilantBackend(JubilantClientStub(client=stub))

            # WHEN wait_for_model_to_exist is called
            backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=10))

            # THEN status was called exactly once and the method returned without error
            assert stub.call_count == 1

        def test_retries_on_model_not_found_then_succeeds(self) -> None:
            # GIVEN a backend whose status() raises "not found" twice then succeeds
            stub = ModelExistsStub(
                error_stderr="ERROR model my-model not found\n",
                max_errors=2,
            )
            backend = JubilantBackend(JubilantClientStub(client=stub))

            # WHEN wait_for_model_to_exist is called (sleep patched to avoid real delays)
            with patch("juju_jubilant.backend.time.sleep"):
                backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=10))

            # THEN status was called three times (2 failures + 1 success)
            assert stub.call_count == 3

        def test_retries_on_model_migrating(self) -> None:
            # GIVEN a backend whose status() raises a "has been migrated to controller" error once
            stub = ModelExistsStub(
                error_stderr="ERROR model my-model has been migrated to controller other-ctrl\n",
                max_errors=1,
            )
            backend = JubilantBackend(JubilantClientStub(client=stub))

            # WHEN wait_for_model_to_exist is called
            with patch("juju_jubilant.backend.time.sleep"):
                backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=10))

            # THEN the migration error was swallowed and status was retried until success
            assert stub.call_count == 2

        def test_retries_on_migration_in_progress(self) -> None:
            # GIVEN a backend whose status() raises a "migration in progress" error once
            stub = ModelExistsStub(
                error_stderr="ERROR migration in progress for model my-model\n",
                max_errors=1,
            )
            backend = JubilantBackend(JubilantClientStub(client=stub))

            # WHEN wait_for_model_to_exist is called
            with patch("juju_jubilant.backend.time.sleep"):
                backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=10))

            # THEN the migration-in-progress error was swallowed and status was retried
            assert stub.call_count == 2

        def test_reraises_unrecognized_cli_error(self) -> None:
            # GIVEN a backend whose status() raises a CLIError unrelated to model availability
            stub = ModelExistsStub(
                error_stderr="ERROR connection to controller lost\n",
                max_errors=0,
            )
            backend = JubilantBackend(JubilantClientStub(client=stub))

            # WHEN / THEN the unrecognized CLIError is re-raised immediately
            with pytest.raises(jubilant.CLIError):
                backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=10))

            # AND status was only called once (no retries)
            assert stub.call_count == 1

        def test_raises_juju_wait_timeout_error_on_timeout(self) -> None:
            # GIVEN a backend whose status() always raises "not found"
            stub = ModelExistsStub(error_stderr="ERROR model my-model not found\n", max_errors=0)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            t0 = datetime(2025, 1, 1, 0, 0, 0)
            # WHEN wait_for_model_to_exist is called with datetime mocked to jump past the timeout
            with patch("juju_jubilant.backend.datetime") as mock_dt, patch("juju_jubilant.backend.time.sleep"):
                mock_dt.now.side_effect = [
                    t0,  # start
                    t0,  # iteration_start, loop 1 — within timeout
                    t0,  # elapsed, loop 1
                    t0 + timedelta(seconds=31),  # iteration_start, loop 2 — past timeout
                ]

                # THEN JujuWaitTimeoutError is raised and its message names the model
                with pytest.raises(JujuWaitTimeoutError) as exc_info:
                    backend.wait_for_model_to_exist("my-model", timeout=timedelta(seconds=30))

            assert "my-model" in exc_info.value.wait_state.message

        def test_uses_default_timeout_when_none_given(self) -> None:
            # GIVEN a backend whose status() always raises "not found"
            stub = ModelExistsStub(error_stderr="ERROR model my-model not found\n", max_errors=0)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            t0 = datetime(2025, 1, 1, 0, 0, 0)
            # WHEN wait_for_model_to_exist is called with timeout=None and datetime mocked to jump
            # past the default_timeout (5 minutes)
            with patch("juju_jubilant.backend.datetime") as mock_dt, patch("juju_jubilant.backend.time.sleep"):
                mock_dt.now.side_effect = [
                    t0,  # start
                    t0,  # iteration_start, loop 1 — within timeout
                    t0,  # elapsed, loop 1
                    t0 + timedelta(minutes=6),  # iteration_start, loop 2 — past default 5-min timeout
                ]

                # THEN JujuWaitTimeoutError is raised, confirming the default timeout was used
                with pytest.raises(JujuWaitTimeoutError) as exc_info:
                    backend.wait_for_model_to_exist("my-model", timeout=None)

            assert "my-model" in exc_info.value.wait_state.message

    class TestWaitForApplicationRevision:
        def test_calls_wait_with_correct_parameters(self) -> None:
            # GIVEN a backend with mocked wait method
            wait_stub = WaitStub()
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN wait_for_application_revision is called
            backend.wait_for_application_revision(
                "myapp",
                expected_revision=42,
                timeout=timedelta(seconds=30),
                model="test-model",
            )

            # THEN wait was called exactly once
            assert wait_stub.call_count == 1

        def test_timeout_raises_error(self) -> None:
            # GIVEN a backend whose wait raises a timeout
            wait_stub = WaitStub(raise_timeout=True)
            backend = JubilantBackend()
            backend.wait = wait_stub.wait

            # WHEN wait_for_application_revision is called
            # THEN JujuWaitTimeoutError is raised
            with pytest.raises(JujuWaitTimeoutError):
                backend.wait_for_application_revision(
                    "myapp",
                    expected_revision=42,
                    timeout=timedelta(seconds=5),
                    model="test-model",
                )

        def test_ready_callback_with_matching_revision(self) -> None:
            # GIVEN a backend with a status that has the target application at the target revision
            stub = StatusStub(
                application_statuses={"myapp": "active"},
            )
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Capture the ready callback
            captured_ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None

            def capture_wait(
                model: str,
                ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
                **kwargs: Any,
            ) -> None:
                nonlocal captured_ready
                captured_ready = ready

            with patch.object(backend, "wait", side_effect=capture_wait):
                backend.wait_for_application_revision(
                    "myapp",
                    expected_revision=42,
                    timeout=timedelta(seconds=5),
                    model="test-model",
                )

            # WHEN the ready callback is evaluated with a status where the app has the correct revision
            assert captured_ready is not None
            ready_status = jubilant.Status(
                model=jubilant.statustypes.ModelStatus(
                    name="test-model",
                    type="caas",
                    controller="test",
                    cloud="test",
                    version="3.0.0",
                ),
                machines={},
                apps={
                    "myapp": jubilant.statustypes.AppStatus(
                        charm="myapp-charm",
                        charm_rev=42,
                        exposed=False,
                        app_status=jubilant.statustypes.StatusInfo(current="active", message=""),
                        units={},
                        relations={},
                        endpoint_bindings={},
                        charm_name="myapp-charm",
                        charm_origin="charmhub",
                    )
                },
            )
            result, _ = captured_ready(ready_status)

            # THEN the callback returns True
            assert result is True

        def test_ready_callback_with_mismatched_revision(self) -> None:
            # GIVEN a backend with a status that has the target application at a different revision
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Capture the ready callback
            captured_ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None

            def capture_wait(
                model: str,
                ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
                **kwargs: Any,
            ) -> None:
                nonlocal captured_ready
                captured_ready = ready

            with patch.object(backend, "wait", side_effect=capture_wait):
                backend.wait_for_application_revision(
                    "myapp",
                    expected_revision=42,
                    timeout=timedelta(seconds=5),
                    model="test-model",
                )

            # WHEN the ready callback is evaluated with a status where the app has a different revision
            assert captured_ready is not None
            ready_status = jubilant.Status(
                model=jubilant.statustypes.ModelStatus(
                    name="test-model",
                    type="caas",
                    controller="test",
                    cloud="test",
                    version="3.0.0",
                ),
                machines={},
                apps={
                    "myapp": jubilant.statustypes.AppStatus(
                        charm="myapp-charm",
                        charm_rev=41,
                        exposed=False,
                        app_status=jubilant.statustypes.StatusInfo(current="active", message=""),
                        units={},
                        relations={},
                        endpoint_bindings={},
                        charm_name="myapp-charm",
                        charm_origin="charmhub",
                    )
                },
            )
            result, _ = captured_ready(ready_status)

            # THEN the callback returns False
            assert result is False

        def test_ready_callback_with_missing_application(self) -> None:
            # GIVEN a backend with an empty model (no applications)
            stub = StatusStub()
            client = JubilantClientStub(client=stub)
            backend = JubilantBackend(client)

            # Capture the ready callback
            captured_ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None

            def capture_wait(
                model: str,
                ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
                **kwargs: Any,
            ) -> None:
                nonlocal captured_ready
                captured_ready = ready

            with patch.object(backend, "wait", side_effect=capture_wait):
                backend.wait_for_application_revision(
                    "myapp",
                    expected_revision=42,
                    timeout=timedelta(seconds=5),
                    model="test-model",
                )

            # WHEN the ready callback is evaluated with a status where the app is missing
            assert captured_ready is not None
            ready_status = jubilant.Status(
                model=jubilant.statustypes.ModelStatus(
                    name="test-model",
                    type="caas",
                    controller="test",
                    cloud="test",
                    version="3.0.0",
                ),
                machines={},
                apps={},  # No applications
            )
            result, _ = captured_ready(ready_status)

            # THEN the callback returns False
            assert result is False

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

            def deploy(self, charm: str, app: str | None = None, config: Any = None, trust: bool = False) -> None:
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
        class ModelStub:
            type: str = "kubernetes"

        @dataclass
        class ScpStub:
            args: list[str] = field(default_factory=list)
            model: Any = None

            def show_model(self) -> Any:
                return self.model

            def cli(self, *args: str) -> None:
                self.args = list(args)

        def test_k8s_model(self, tmp_path: Path) -> None:
            # GIVEN
            stub = self.ScpStub()
            stub.model = self.ModelStub(type="kubernetes")
            client = JubilantClientStub(client=stub)
            source = tmp_path / "a"
            source.write_text("")

            # WHEN
            with patch("pathlib.Path.home", return_value=tmp_path):
                JubilantBackend(client).scp("test-model", source=str(source), destination="b")

            # THEN
            assert stub.args == ["scp", str(source), "b"]

        def test_non_k8s_model_specifies_recursive(self, tmp_path: Path) -> None:
            # GIVEN
            stub = self.ScpStub()
            stub.model = self.ModelStub(type="not-kubernetes")
            client = JubilantClientStub(client=stub)
            source = tmp_path / "a"
            source.write_text("")

            # WHEN
            with patch("pathlib.Path.home", return_value=tmp_path):
                JubilantBackend(client).scp("test-model", source=str(source), destination="b")

            # THEN
            assert stub.args == ["scp", "--", "-r", str(source), "b"]

        def test_out_of_home_file_is_staged(self, tmp_path: Path) -> None:
            # GIVEN a source file outside $HOME (e.g. /project/validator.py)
            home_dir = tmp_path / "home"
            home_dir.mkdir()
            source_file = tmp_path / "project" / "validator.py"
            source_file.parent.mkdir()
            source_file.write_text("# content")

            stub = self.ScpStub()
            stub.model = self.ModelStub(type="kubernetes")
            client = JubilantClientStub(client=stub)

            # WHEN
            with patch("pathlib.Path.home", return_value=home_dir):
                JubilantBackend(client).scp("test-model", source=str(source_file), destination="unit/0:/tmp/")

            # THEN scp was called with a staged copy inside home_dir with the same filename
            assert stub.args[0] == "scp"
            staged_source = Path(stub.args[-2])
            assert staged_source.is_relative_to(home_dir)
            assert staged_source.name == source_file.name

        def test_out_of_home_directory_is_staged(self, tmp_path: Path) -> None:
            # GIVEN a source directory outside $HOME (e.g. /project/mypackage/)
            home_dir = tmp_path / "home"
            home_dir.mkdir()
            source_dir = tmp_path / "project" / "mypackage"
            source_dir.mkdir(parents=True)
            (source_dir / "file.py").write_text("# content")

            stub = self.ScpStub()
            stub.model = self.ModelStub(type="kubernetes")
            client = JubilantClientStub(client=stub)

            # WHEN
            with patch("pathlib.Path.home", return_value=home_dir):
                JubilantBackend(client).scp("test-model", source=str(source_dir), destination="unit/0:/tmp/")

            # THEN scp was called with a staged copy inside home_dir with the same directory name
            assert stub.args[0] == "scp"
            staged_source = Path(stub.args[-2])
            assert staged_source.is_relative_to(home_dir)
            assert staged_source.name == source_dir.name

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

    class TestExecUnit:
        @staticmethod
        def _exec_yaml(unit: str, return_code: int, stdout: str = "", stderr: str = "") -> str:
            return yaml.dump(
                {
                    unit: {
                        "id": "1",
                        "status": "completed",
                        "results": {
                            "return-code": return_code,
                            "stdout": stdout,
                            "stderr": stderr,
                        },
                    }
                }
            )

        @dataclass
        class ExecCliStub:
            calls: list[tuple[str, ...]] = field(default_factory=list)
            response: str = ""
            fail_with_task_error: bool = False
            juju_version: str = "3.6.1-ubuntu-amd64"

            def cli(self, *args: str) -> str:
                self.calls.append(tuple(args))
                if self.fail_with_task_error:
                    raise jubilant.CLIError(1, ["juju"], self.response, "ERROR the following task failed")
                return self.response

            def version(self) -> str:
                return self.juju_version

        # Stub that provides both cli() (for exec) and status() (for leader resolution).
        class ExecAndStatusStub:
            class _UnitInfo:
                def __init__(self, leader: bool) -> None:
                    self.leader = leader

            class _AppStatus:
                def __init__(self, units: dict[str, Any]) -> None:
                    self.units = units

            class _Status:
                def __init__(self, apps: dict[str, Any]) -> None:
                    self.apps = apps

            def __init__(self, cli_response: str, leader_units: dict[str, str]) -> None:
                """
                cli_response: YAML string to return from cli()
                leader_units: mapping of app_name -> leader unit name (e.g. {"myapp": "myapp/1"})
                """
                self.cli_response = cli_response
                self.leader_units = leader_units
                self.cli_calls: list[tuple[str, ...]] = []

            def cli(self, *args: str) -> str:
                self.cli_calls.append(tuple(args))
                return self.cli_response

            def status(self) -> Any:
                apps = {}
                for app_name, leader_unit in self.leader_units.items():
                    # Build a two-unit app where the specified unit is the leader.
                    other_unit = f"{app_name}/0" if leader_unit != f"{app_name}/0" else f"{app_name}/1"
                    apps[app_name] = self._AppStatus(
                        {
                            other_unit: self._UnitInfo(leader=False),
                            leader_unit: self._UnitInfo(leader=True),
                        }
                    )
                return self._Status(apps)

        def test_exec_success(self) -> None:
            # GIVEN a successful exec
            stub = self.ExecCliStub(response=self._exec_yaml("myapp/0", 0, stdout="hello\n"))
            client = JubilantClientStub(client=stub)

            # WHEN
            result = JubilantBackend(client).exec_unit("test-model", "myapp/0", "echo hello")

            # THEN _exec prepends "exec", "--format", "yaml" before the unit args
            assert stub.calls == [("exec", "--format", "yaml", "--unit", "myapp/0", "--", "echo hello")]
            assert result.return_code == 0
            assert result.stdout == "hello\n"
            assert result.stderr == ""

        def test_exec_with_operator(self) -> None:
            # GIVEN a Juju 3 backend
            stub = self.ExecCliStub(
                response=self._exec_yaml("myapp/0", 0, stdout="hello\n"),
                juju_version="3.6.1-ubuntu-amd64",
            )
            client = JubilantClientStub(client=stub)

            # WHEN operator=True is passed
            result = JubilantBackend(client).exec_unit("test-model", "myapp/0", "echo hello", operator=True)

            # THEN --operator appears after --unit but before "--"
            assert stub.calls == [("exec", "--format", "yaml", "--unit", "myapp/0", "--operator", "--", "echo hello")]
            assert result.return_code == 0

        def test_exec_with_operator_on_juju4_warns(self) -> None:
            # GIVEN a Juju 4 backend
            stub = self.ExecCliStub(
                response=self._exec_yaml("myapp/0", 0, stdout="hello\n"),
                juju_version="4.0.0-ubuntu-amd64",
            )
            client = JubilantClientStub(client=stub)

            # WHEN operator=True is passed
            import warnings

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                JubilantBackend(client).exec_unit("test-model", "myapp/0", "echo hello", operator=True)

            # THEN a warning is emitted and --operator is NOT passed
            assert len(caught) == 1
            assert "--operator" in str(caught[0].message)
            assert stub.calls == [("exec", "--format", "yaml", "--unit", "myapp/0", "--", "echo hello")]

        def test_exec_nonzero_return_code(self) -> None:
            # GIVEN a command that exits non-zero (juju exec raises CLIError with "task failed")
            stub = self.ExecCliStub(
                response=self._exec_yaml("myapp/0", 1, stderr="command not found"),
                fail_with_task_error=True,
            )
            client = JubilantClientStub(client=stub)

            # WHEN
            result = JubilantBackend(client).exec_unit("test-model", "myapp/0", "bad-cmd")

            # THEN the non-zero return code is returned, not raised
            assert result.return_code == 1
            assert result.stderr == "command not found"

        def test_exec_with_leader_resolves_via_status(self) -> None:
            # GIVEN: output is keyed by the resolved unit "myapp/1" (juju replaces "leader" in output)
            stub = self.ExecAndStatusStub(
                cli_response=self._exec_yaml("myapp/1", 0, stdout="leader output\n"),
                leader_units={"myapp": "myapp/1"},
            )
            client = JubilantClientStub(client=stub)

            # WHEN exec_unit is called with "app/leader"
            result = JubilantBackend(client).exec_unit("test-model", "myapp/leader", "echo hello")

            # THEN the resolved leader unit's output is returned
            assert result.return_code == 0
            assert result.stdout == "leader output\n"

        def test_exec_leader_not_found_raises(self) -> None:
            # GIVEN a status where no unit has leader=True
            class NoLeaderStub:
                class _UnitInfo:
                    leader = False

                class _AppStatus:
                    def __init__(self) -> None:
                        self.units: dict[str, Any] = {"myapp/0": NoLeaderStub._UnitInfo()}

                class _Status:
                    def __init__(self) -> None:
                        self.apps: dict[str, Any] = {"myapp": NoLeaderStub._AppStatus()}

                def cli(self, *args: str) -> str:
                    # Return valid YAML so _exec succeeds; leader resolution happens after
                    return yaml.dump(
                        {
                            "myapp/0": {
                                "id": "1",
                                "status": "completed",
                                "results": {"return-code": 0, "stdout": "", "stderr": ""},
                            }
                        }
                    )

                def status(self) -> Any:
                    return self._Status()

            client = JubilantClientStub(client=NoLeaderStub())

            # WHEN / THEN
            with pytest.raises(KeyError, match="No leader found for application 'myapp'"):
                JubilantBackend(client).exec_unit("test-model", "myapp/leader", "echo hello")

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

    class TestListApplications:
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
                self.apps = TestJubilantBackend.TestListApplications.StatusStub(charm, charm_rev).apps

        class StatusStubClient:
            def status(self) -> "TestJubilantBackend.TestListApplications.ModelStatus":
                return TestJubilantBackend.TestListApplications.ModelStatus("my-charm", 1)

        class ModelStub:
            client: "TestJubilantBackend.TestListApplications.StatusStubClient"

            def __init__(self, client: "TestJubilantBackend.TestListApplications.StatusStubClient") -> None:
                self.client = client

            def status(self) -> "TestJubilantBackend.TestListApplications.ModelStatus":
                return self.client.status()

        def test(self) -> None:
            # GIVEN
            client = JubilantClientStub(client=self.ModelStub(client=self.StatusStubClient()))

            # WHEN
            applications = JubilantBackend(client).list_applications("test-model")

            # THEN
            assert len(applications) == 1
            app_info = applications["my-app"]
            assert app_info.charm == "my-charm"
            assert app_info.revision == 1
            assert app_info.channel is None

        def test_with_channel(self) -> None:
            # GIVEN
            class ModelStatusWithChannel:
                def __init__(self) -> None:
                    self.apps = {
                        "my-app": jubilant.statustypes.AppStatus(
                            charm="my-charm",
                            charm_origin="charmhub",
                            charm_name="my-charm",
                            charm_rev=1,
                            charm_channel="1.0/stable",
                            exposed=False,
                        )
                    }

            class StatusStubClientWithChannel:
                def status(self) -> ModelStatusWithChannel:
                    return ModelStatusWithChannel()

            class ModelStubWithChannel:
                def __init__(self) -> None:
                    self.client = StatusStubClientWithChannel()

                def status(self) -> ModelStatusWithChannel:
                    return self.client.status()

            client = JubilantClientStub(client=ModelStubWithChannel())

            # WHEN
            applications = JubilantBackend(client).list_applications("test-model")

            # THEN
            assert len(applications) == 1
            app_info = applications["my-app"]
            assert app_info.charm == "my-charm"
            assert app_info.revision == 1
            assert app_info.channel == CharmChannel(track="1.0", risk="stable", branch="")

    class TestListConsumedOffers:
        class Client(JubilantClientStub):
            def __init__(self) -> None:
                super().__init__(client=self)

            def status(self) -> Any:
                return self

            @property
            def app_endpoints(self) -> dict[str, jubilant.statustypes.RemoteAppStatus]:
                return {
                    "consumed-offer": jubilant.statustypes.RemoteAppStatus(
                        url="neighbor-controller:admin/neighbor-model.neighbor-offer"
                    )
                }

        def test(self) -> None:
            # GIVEN
            client = self.Client()

            # WHEN
            consumed_offers = JubilantBackend(client).list_consumed_offers("ignored-in-stub")

            # THEN
            assert consumed_offers == {
                "consumed-offer": JujuConsumedOfferInfo(url="neighbor-controller:admin/neighbor-model.neighbor-offer")
            }

    class TestListIntegrations:
        class CliStub:
            def __init__(self, status_output: str) -> None:
                self.status_output = status_output

            def cli(self, *args: str) -> str:
                if args == ("status", "--integrations", "--format", "tabular"):
                    return self.status_output
                return ""

        @dataclass
        class Params:
            label: str
            status_output: str
            expected_count: int
            expected_integrations: list[dict[str, str]] = field(default_factory=list)

        test_cases = [
            Params(
                label="parses_tabular_status",
                status_output=STATUS_WITH_SINGLE_INTEGRATION,
                expected_count=1,
                expected_integrations=[
                    {
                        "provider_app": "database",
                        "provider_endpoint": "db",
                        "requirer_app": "webapp",
                        "requirer_endpoint": "database",
                        "interface": "pgsql",
                    }
                ],
            ),
            Params(
                label="skips_peer_integrations",
                status_output=STATUS_WITH_PEER_INTEGRATION,
                expected_count=0,
            ),
            Params(
                label="handles_multiple_integrations",
                status_output=STATUS_WITH_MULTIPLE_INTEGRATIONS,
                expected_count=2,
                expected_integrations=[
                    {
                        "provider_app": "cache",
                        "provider_endpoint": "cache",
                        "requirer_app": "webapp",
                        "requirer_endpoint": "redis",
                        "interface": "redis",
                    },
                    {
                        "provider_app": "database",
                        "provider_endpoint": "db",
                        "requirer_app": "webapp",
                        "requirer_endpoint": "database",
                        "interface": "pgsql",
                    },
                ],
            ),
            Params(
                label="returns_empty_set_when_no_integrations",
                status_output=STATUS_WITH_NO_INTEGRATIONS,
                expected_count=0,
            ),
            Params(
                label="empty_model",
                status_output=STATUS_EMPTY_MODEL,
                expected_count=0,
            ),
            Params(
                label="model_with_app_but_no_integration_section",
                status_output=STATUS_NO_INTEGRATION_SECTION,
                expected_count=0,
            ),
            Params(
                label="collects_multiple_integrations_with_message",
                status_output=STATUS_WITH_MULTIPLE_INTEGRATIONS_MESSAGE,
                expected_count=2,
                expected_integrations=[
                    {
                        "provider_app": "cache",
                        "provider_endpoint": "cache",
                        "requirer_app": "webapp",
                        "requirer_endpoint": "redis",
                        "interface": "redis",
                    },
                    {
                        "provider_app": "database",
                        "provider_endpoint": "db",
                        "requirer_app": "webapp",
                        "requirer_endpoint": "database",
                        "interface": "pgsql",
                    },
                ],
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a client with status output
            client = JubilantClientStub(client=self.CliStub(params.status_output))

            # WHEN list_integrations is called
            integrations = JubilantBackend(client).list_integrations("test-model")

            # THEN count matches expected
            assert len(integrations) == params.expected_count
            assert isinstance(integrations, set)

            # AND integration details match expected (if any)
            if params.expected_integrations:
                integrations_list = sorted(integrations, key=lambda i: i.provider.application)
                for i, expected in enumerate(params.expected_integrations):
                    assert integrations_list[i].provider.application == expected["provider_app"]
                    assert integrations_list[i].provider.endpoint == expected["provider_endpoint"]
                    assert integrations_list[i].requirer.application == expected["requirer_app"]
                    assert integrations_list[i].requirer.endpoint == expected["requirer_endpoint"]
                    assert integrations_list[i].interface == expected["interface"]

    class TestIntegrationExists:
        class CliStub:
            def __init__(self, status_output: str) -> None:
                self.status_output = status_output

            def cli(self, *args: str) -> str:
                if args == ("status", "--integrations", "--format", "tabular"):
                    return self.status_output
                return ""

        @dataclass
        class Params:
            label: str
            status_output: str
            app1: str
            endpoint1: str
            app2: str
            endpoint2: str
            expected_exists: bool

        test_cases = [
            Params(
                label="integration_exists",
                status_output=STATUS_WITH_SINGLE_INTEGRATION,
                app1="database",
                endpoint1="db",
                app2="webapp",
                endpoint2="database",
                expected_exists=True,
            ),
            Params(
                label="integration_exists_reversed_direction",
                status_output=STATUS_WITH_SINGLE_INTEGRATION,
                app1="webapp",
                endpoint1="database",
                app2="database",
                endpoint2="db",
                expected_exists=True,
            ),
            Params(
                label="integration_does_not_exist",
                status_output=STATUS_WITH_SINGLE_INTEGRATION,
                app1="cache",
                endpoint1="cache",
                app2="webapp",
                endpoint2="redis",
                expected_exists=False,
            ),
            Params(
                label="partial_match_wrong_endpoint",
                status_output=STATUS_WITH_SINGLE_INTEGRATION,
                app1="database",
                endpoint1="wrong-endpoint",
                app2="webapp",
                endpoint2="database",
                expected_exists=False,
            ),
            Params(
                label="empty_model",
                status_output=STATUS_EMPTY_MODEL,
                app1="database",
                endpoint1="db",
                app2="webapp",
                endpoint2="database",
                expected_exists=False,
            ),
            Params(
                label="multiple_integrations_finds_correct_one",
                status_output=STATUS_WITH_MULTIPLE_INTEGRATIONS,
                app1="cache",
                endpoint1="cache",
                app2="webapp",
                endpoint2="redis",
                expected_exists=True,
            ),
            Params(
                label="multiple_integrations_with_messages",
                status_output=STATUS_WITH_MULTIPLE_INTEGRATIONS_MESSAGE,
                app1="cache",
                endpoint1="cache",
                app2="webapp",
                endpoint2="redis",
                expected_exists=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a client with status output
            client = JubilantClientStub(client=self.CliStub(params.status_output))

            # WHEN integration_exists is called
            exists = JubilantBackend(client).integration_exists(
                params.app1, params.endpoint1, params.app2, params.endpoint2, "test-model"
            )

            # THEN result matches expected
            assert exists == params.expected_exists

    class TestRunAction:
        @dataclass
        class ActionStub:
            unit: str = ""
            action: str = ""
            params: dict[str, Any] = field(default_factory=dict)

            def run(self, unit: str, action: str, params: dict[str, Any]) -> jubilant.Task:
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

    class TestControllerSetupRetries:
        @dataclass
        class SetupStub:
            bootstrap_failures_remaining: int = 0
            add_model_failures_remaining: int = 0
            bootstrap_calls: int = 0
            add_model_calls: int = 0
            switch_calls: int = 0

            def bootstrap(
                self,
                cloud: str,
                controller: str,
                bootstrap_constraints: dict[str, str],
                metadata_source: str | None = None,
                config: dict[str, str] | None = None,
            ) -> None:
                self.bootstrap_calls += 1
                if self.bootstrap_failures_remaining > 0:
                    self.bootstrap_failures_remaining -= 1
                    raise RuntimeError("transient bootstrap failure")

            def add_model(self, model: str, controller: str, config: dict[str, str]) -> None:
                self.add_model_calls += 1
                if self.add_model_failures_remaining > 0:
                    self.add_model_failures_remaining -= 1
                    raise RuntimeError("transient add-model failure")

            def cli(self, *args: str, include_model: bool = True) -> str:
                if args and args[0] == "switch":
                    self.switch_calls += 1
                return ""

        def test_bootstrap_controller_retries_then_succeeds(self) -> None:
            stub = self.SetupStub(bootstrap_failures_remaining=2)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            with patch("tenacity.nap.sleep", return_value=None):
                backend.bootstrap_controller(
                    cloud="k8s-stg", controller="test-controller", controller_constraints={}, bootstrap_configuration={}
                )

            assert stub.bootstrap_calls == 3

        def test_bootstrap_controller_retries_then_raises(self) -> None:
            stub = self.SetupStub(bootstrap_failures_remaining=3)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            with patch("tenacity.nap.sleep", return_value=None):
                with pytest.raises(RuntimeError, match="transient bootstrap failure"):
                    backend.bootstrap_controller(
                        cloud="k8s-stg",
                        controller="test-controller",
                        controller_constraints={},
                        bootstrap_configuration={},
                    )

            assert stub.bootstrap_calls == 3

        def test_add_model_retries_then_succeeds(self) -> None:
            stub = self.SetupStub(add_model_failures_remaining=2)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            with patch("tenacity.nap.sleep", return_value=None):
                backend.add_model(controller="test-controller", model="test-model", model_config={})

            assert stub.add_model_calls == 3
            assert stub.switch_calls == 0

        def test_add_model_retries_then_raises(self) -> None:
            stub = self.SetupStub(add_model_failures_remaining=3)
            backend = JubilantBackend(JubilantClientStub(client=stub))

            with patch("tenacity.nap.sleep", return_value=None):
                with pytest.raises(RuntimeError, match="transient add-model failure"):
                    backend.add_model(controller="test-controller", model="test-model", model_config={})

            assert stub.add_model_calls == 3
            assert stub.switch_calls == 0


class TestParseBundleFile:
    def test_flat_relations(self, tmp_path: Path) -> None:
        # GIVEN a bundle with flat-list relations
        bundle_content: dict[str, Any] = {
            "applications": {"database": {}, "webapp": {}},
            "relations": [["database:db", "webapp:database"]],
        }
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump(bundle_content))

        # WHEN
        app_names, integrations = _parse_bundle(str(bundle_file))

        # THEN
        assert set(app_names) == {"database", "webapp"}
        assert integrations == [
            (JujuIntegrationApplication("database", "db"), JujuIntegrationApplication("webapp", "database"))
        ]

    def test_nested_relations(self, tmp_path: Path) -> None:
        # GIVEN a bundle with nested-list relations (alternative YAML format)
        bundle_content: dict[str, Any] = {
            "applications": {"database": {}, "webapp": {}},
            "relations": [[["database:db"], ["webapp:database"]]],
        }
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump(bundle_content))

        # WHEN
        _, integrations = _parse_bundle(str(bundle_file))

        # THEN
        assert integrations == [
            (JujuIntegrationApplication("database", "db"), JujuIntegrationApplication("webapp", "database"))
        ]

    def test_no_relations(self, tmp_path: Path) -> None:
        # GIVEN a bundle with applications but no relations key
        bundle_content: dict[str, Any] = {"applications": {"app1": {}}}
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump(bundle_content))

        # WHEN
        app_names, integrations = _parse_bundle(str(bundle_file))

        # THEN
        assert app_names == ["app1"]
        assert integrations == []

    def test_empty_bundle(self, tmp_path: Path) -> None:
        # GIVEN an empty bundle
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump({}))

        # WHEN
        app_names, integrations = _parse_bundle(str(bundle_file))

        # THEN
        assert app_names == []
        assert integrations == []


class TestDeployBundleFile:
    def test_calls_wait_once_with_combined_predicate(self, tmp_path: Path) -> None:
        # GIVEN a bundle with apps and one integration
        bundle_content: dict[str, Any] = {
            "applications": {"database": {}, "webapp": {}},
            "relations": [["database:db", "webapp:database"]],
        }
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump(bundle_content))

        client = JubilantClientStub(client=StatusStub())
        backend = JubilantBackend(client)
        wait_calls: list[Any] = []

        with (
            patch("juju_jubilant.backend.JujuCmdBackend.deploy_bundle_file"),
            patch.object(backend, "wait", side_effect=lambda *a, **kw: wait_calls.append(a)),
        ):
            backend.deploy_bundle_file("test-model", str(bundle_file))

        # THEN wait is called exactly once (combined predicate)
        assert len(wait_calls) == 1

    def test_combined_predicate_passes_when_apps_active_and_integrations_present(self, tmp_path: Path) -> None:
        # GIVEN a bundle with one app and no integrations
        bundle_content: dict[str, Any] = {"applications": {"database": {}}}
        bundle_file = tmp_path / "bundle.yaml"
        bundle_file.write_text(yaml.dump(bundle_content))

        stub = StatusStub(
            application_statuses={"database": "active"},
            unit_workload_statuses={"database/0": "active"},
            unit_juju_statuses={"database/0": "idle"},
        )
        client = JubilantClientStub(client=stub)
        backend = JubilantBackend(client)
        captured_ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None

        def capture_wait(
            model: str,
            ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
            **kwargs: Any,
        ) -> None:
            nonlocal captured_ready
            captured_ready = ready

        with (
            patch("juju_jubilant.backend.JujuCmdBackend.deploy_bundle_file"),
            patch.object(backend, "wait", side_effect=capture_wait),
        ):
            backend.deploy_bundle_file("test-model", str(bundle_file))

        # WHEN the predicate is evaluated against a settled status
        assert captured_ready is not None
        result, _ = captured_ready(stub.status())

        # THEN the predicate returns True
        assert result is True


class TestJubilantBackendVersion:
    @dataclass
    class VersionStub:
        version_str: str = "3.6.1-ubuntu-amd64"

        def status(self) -> jubilant.Status:
            return jubilant.Status(
                model=jubilant.statustypes.ModelStatus(
                    name="test-model",
                    type="caas",
                    controller="test",
                    cloud="test",
                    version=self.version_str,
                ),
                machines={},
                apps={},
            )

    def test_returns_version_from_model(self) -> None:
        # GIVEN a client stub whose status() exposes a known version
        client = JubilantClientStub(client=self.VersionStub(version_str="3.6.1-ubuntu-amd64"))
        backend = JubilantBackend(client)

        # WHEN
        result = backend.version("test-model")

        # THEN the version is parsed and returned as a JujuVersion
        assert result == JujuVersion(3, 6, 1)


class TestJubilantBackendCliVersion:
    @dataclass
    class VersionStub:
        version_str: str = "3.6.1-ubuntu-amd64"

        def version(self) -> str:
            return self.version_str

    def test_returns_cli_version_stripped(self) -> None:
        # GIVEN a client stub that returns a version with surrounding whitespace
        client = JubilantClientStub(client=self.VersionStub(version_str="  3.6.1-ubuntu-amd64  "))
        backend = JubilantBackend(client)

        # WHEN
        result = backend.cli_version()

        # THEN the version is parsed and returned as a JujuVersion
        assert result == JujuVersion(3, 6, 1)

    def test_passes_none_model_to_client(self) -> None:
        # GIVEN a client stub that records which model was requested
        requested_models: list[str | None] = []
        version_stub = self.VersionStub()

        class TrackingClient(JubilantClientStub):
            def model(self, model: str | None) -> Any:
                requested_models.append(model)
                return version_stub

        backend = JubilantBackend(TrackingClient(client=version_stub))

        # WHEN
        backend.cli_version()

        # THEN model(None) was called (no specific model - CLI-level version)
        assert None in requested_models


class TestJubilantBackendDebugLog:
    def test_debug_log_calls_client_debug_log(self) -> None:
        # GIVEN a client stub that returns a debug log message
        class ModelStub:
            def __init__(self, model: str) -> None:
                self.model = model

            def debug_log(self) -> str:
                return f"this is a debug log for model {self.model}"

        class DebugClient(JubilantClientStub):
            def model(self, model: str | Any) -> Any:
                return ModelStub(model=model)

        client = DebugClient(client=None)
        backend = JubilantBackend(client)

        # WHEN we call debug_log on the backend
        log = backend.debug_log("my-model")

        # THEN the client's debug_log message from the client is returned
        assert log == "this is a debug log for model my-model"
