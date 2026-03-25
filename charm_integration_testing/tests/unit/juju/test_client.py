# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any

import pytest
from juju import JujuValidationError
from juju.client import JujuClient
from juju.extension import JujuExtension
from juju.models import JujuApplicationInfo

from validators.base.validator import ValidationCheck, ValidationResult

from ..extensions.shared import NullJujuBackend

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class LoggerStub:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def info(self, msg: str, *args: object, **kwargs: object) -> None:
        self.infos.append(str(msg))

    def error(self, msg: str, *args: object, **kwargs: object) -> None:
        self.errors.append(str(msg))

    def debug(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def getChild(self, suffix: str) -> "LoggerStub":
        return self


@dataclass
class BackendStub(NullJujuBackend):
    """Backend that returns a fixed application list and configurable validate results."""

    app_list: dict[str, JujuApplicationInfo] = field(default_factory=dict)
    validate_results: dict[str, dict[str, list[ValidationResult]]] = field(default_factory=dict)

    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
        return self.app_list

    def validate_application(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
        return self.validate_results.get(application, {})


@dataclass
class KillControllerBackendStub(NullJujuBackend):
    """Backend stub that records kill_controller calls."""

    killed_controllers: list[str] = field(default_factory=list)

    def kill_controller(self, controller: str) -> None:
        self.killed_controllers.append(controller)


@dataclass
class MigrateModelBackendStub(NullJujuBackend):
    """Backend stub that records migrate_model calls."""

    migrated_models: list[tuple[str, str, str]] = field(default_factory=list)

    def migrate_model(self, model_name: str, source_controller: str, target_controller: str) -> None:
        self.migrated_models.append((model_name, source_controller, target_controller))


class ExtensionStub(JujuExtension):
    """Extension that returns configurable validate results."""

    def __init__(self, results: dict[str, dict[str, list[ValidationResult]]]) -> None:
        self.results = results

    def post_validate(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
        return self.results.get(application, {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pass(endpoint: str = "db", interface: str = "postgresql_client") -> ValidationResult:
    return ValidationResult(status="PASS", endpoint=endpoint, interface=interface, level="simple", relation_id=0)


def _fail(endpoint: str = "db", interface: str = "postgresql_client") -> ValidationResult:
    return ValidationResult(status="FAIL", endpoint=endpoint, interface=interface, level="simple", relation_id=0)


def _fail_with_check(
    endpoint: str = "db", check_name: str = "connect", check_message: str = "timeout"
) -> ValidationResult:
    return ValidationResult(
        status="FAIL",
        endpoint=endpoint,
        interface="postgresql_client",
        level="simple",
        relation_id=0,
        checks=[ValidationCheck(name=check_name, passed=False, message=check_message)],
    )


def _error(endpoint: str = "db", error: str = "exception occurred") -> ValidationResult:
    return ValidationResult(
        status="ERROR", endpoint=endpoint, interface="postgresql_client", level="simple", relation_id=0, error=error
    )


def _app_info(charm: str = "postgresql") -> JujuApplicationInfo:
    return JujuApplicationInfo(charm=charm, revision=1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJujuValidationError:
    class TestMessage:
        def test_single_unit_single_failure(self) -> None:
            # GIVEN one unit with one failure
            error = JujuValidationError({"myapp/0": [_fail()]})

            # WHEN / THEN
            assert "1 failed validations" in str(error)
            assert "myapp/0" in str(error)

        def test_multiple_units_multiple_failures(self) -> None:
            # GIVEN two units with multiple failures
            error = JujuValidationError({"myapp/0": [_fail()], "myapp/1": [_fail(), _fail("metrics")]})

            # WHEN / THEN
            assert "3 failed validations" in str(error)
            assert "myapp/0" in str(error)
            assert "myapp/1" in str(error)


class TestJujuClientValidateModel:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(
        self,
        logger: Any,
        backend: BackendStub,
        extensions: list[JujuExtension] | None = None,
    ) -> JujuClient:
        return JujuClient(backend, logger, extensions or [])

    def test_does_not_raise_when_no_applications(self, logger: LoggerStub) -> None:
        # GIVEN a model with no applications
        backend = BackendStub(app_list={})
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

    def test_does_not_raise_when_all_pass(self, logger: LoggerStub) -> None:
        # GIVEN one application whose backend validation returns PASS
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_pass()]}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

    def test_raises_when_backend_returns_fail(self, logger: LoggerStub) -> None:
        # GIVEN the backend returns a FAIL result
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_fail()]}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN
        with pytest.raises(JujuValidationError) as exc_info:
            client.validate_model("mymodel")
        assert "myapp/0" in exc_info.value.failed_validations

    def test_raises_when_extension_returns_fail(self, logger: LoggerStub) -> None:
        # GIVEN the backend returns nothing but the extension returns a FAIL
        backend = BackendStub(app_list={"myapp": _app_info()})
        extension = ExtensionStub({"myapp": {"myapp/0": [_fail()]}})
        client = self._client(logger, backend, [extension])

        # WHEN / THEN
        with pytest.raises(JujuValidationError) as exc_info:
            client.validate_model("mymodel")
        assert "myapp/0" in exc_info.value.failed_validations

    def test_merges_backend_and_extension_results_for_same_unit(self, logger: LoggerStub) -> None:
        # GIVEN backend returns one FAIL and extension returns another FAIL for the same unit
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_fail("db")]}},
        )
        extension = ExtensionStub({"myapp": {"myapp/0": [_fail("metrics")]}})
        client = self._client(logger, backend, [extension])

        # WHEN
        with pytest.raises(JujuValidationError) as exc_info:
            client.validate_model("mymodel")

        # THEN both failures are recorded under the same unit
        unit_failures = exc_info.value.failed_validations["myapp/0"]
        endpoints = {r.endpoint for r in unit_failures}
        assert "db" in endpoints
        assert "metrics" in endpoints

    def test_failed_validations_excludes_passing_results(self, logger: LoggerStub) -> None:
        # GIVEN a mix of PASS and FAIL results for the same unit
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_pass("metrics"), _fail("db")]}},
        )
        client = self._client(logger, backend)

        # WHEN
        with pytest.raises(JujuValidationError) as exc_info:
            client.validate_model("mymodel")

        # THEN only the failing result is in failed_validations
        unit_failures = exc_info.value.failed_validations["myapp/0"]
        assert len(unit_failures) == 1
        assert unit_failures[0].endpoint == "db"

    def test_logs_error_for_failing_result(self, logger: LoggerStub) -> None:
        # GIVEN a FAIL result
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_fail("db")]}},
        )
        client = self._client(logger, backend)

        # WHEN
        with pytest.raises(JujuValidationError):
            client.validate_model("mymodel")

        # THEN an error was logged mentioning the endpoint
        assert any("db" in e for e in logger.errors)

    def test_logs_failed_check_name_and_message(self, logger: LoggerStub) -> None:
        # GIVEN a FAIL result with a named failed check
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_fail_with_check("db", "connect", "connection timed out")]}},
        )
        client = self._client(logger, backend)

        # WHEN
        with pytest.raises(JujuValidationError):
            client.validate_model("mymodel")

        # THEN the check name and message are both logged
        assert any("connect" in e and "connection timed out" in e for e in logger.errors)

    def test_logs_error_string_when_present(self, logger: LoggerStub) -> None:
        # GIVEN an ERROR result with an error string
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_error("db", "unexpected exception")]}},
        )
        client = self._client(logger, backend)

        # WHEN
        with pytest.raises(JujuValidationError):
            client.validate_model("mymodel")

        # THEN the error string is logged
        assert any("unexpected exception" in e for e in logger.errors)


class TestJujuClientKillController:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_delegates_to_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records kill_controller calls
        backend = KillControllerBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.kill_controller("mycontroller")

        # THEN the backend received the call with the correct controller name
        assert "mycontroller" in backend.killed_controllers

    def test_logs_controller_name(self, logger: LoggerStub) -> None:
        # GIVEN a backend stub
        backend = KillControllerBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.kill_controller("mycontroller")

        # THEN an info message mentioning the controller was logged
        assert any("mycontroller" in msg for msg in logger.infos)


class TestJujuClientMigrateModel:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_delegates_to_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records migrate_model calls
        backend = MigrateModelBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.migrate_model("mymodel", "source-ctrl", "target-ctrl")

        # THEN the backend received the call with all three arguments
        assert ("mymodel", "source-ctrl", "target-ctrl") in backend.migrated_models

    def test_logs_model_and_controllers(self, logger: LoggerStub) -> None:
        # GIVEN a backend stub
        backend = MigrateModelBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.migrate_model("mymodel", "source-ctrl", "target-ctrl")

        # THEN an info message mentioning the model and both controllers was logged
        assert any("mymodel" in msg and "source-ctrl" in msg and "target-ctrl" in msg for msg in logger.infos)
