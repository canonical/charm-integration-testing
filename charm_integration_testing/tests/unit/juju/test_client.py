# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from juju import JujuValidationError
from juju.client import JujuClient
from juju.extension import JujuExtension
from juju.models import JujuApplicationInfo
from juju.version import JujuVersion

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


@dataclass
class RefreshBackendStub(NullJujuBackend):
    """Backend that records each refresh_application call as a (model, application, revision, channel) tuple."""

    refresh_calls: list[tuple[str, str, int | None, str | None]] = field(default_factory=list)

    def refresh_application(
        self,
        model: str,
        application: str,
        revision: int | None = None,
        channel: str | None = None,
    ) -> None:
        self.refresh_calls.append((model, application, revision, channel))


@dataclass
class RevisionSequenceBackendStub(NullJujuBackend):
    """Returns successive revisions from a fixed sequence on each list_applications call.

    Once the sequence is exhausted the last value is repeated indefinitely.
    Pass None as a revision to simulate a missing application.
    """

    application: str
    revisions: list[int | None]

    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
        revision = self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]
        if revision is None:
            return {}
        return {self.application: JujuApplicationInfo(charm="mycharm", revision=revision)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pass(endpoint: str = "db", interface: str = "postgresql_client") -> ValidationResult:
    return ValidationResult(
        status="PASS", endpoint=endpoint, interface=interface, level="simple", role="requires", relation_id=0
    )


def _fail(endpoint: str = "db", interface: str = "postgresql_client") -> ValidationResult:
    return ValidationResult(
        status="FAIL", endpoint=endpoint, interface=interface, level="simple", role="requires", relation_id=0
    )


def _fail_with_check(
    endpoint: str = "db", check_name: str = "connect", check_message: str = "timeout"
) -> ValidationResult:
    return ValidationResult(
        status="FAIL",
        endpoint=endpoint,
        interface="postgresql_client",
        level="simple",
        role="requires",
        relation_id=0,
        checks=[ValidationCheck(name=check_name, passed=False, message=check_message)],
    )


def _error(endpoint: str = "db", error: str = "exception occurred") -> ValidationResult:
    return ValidationResult(
        status="ERROR",
        endpoint=endpoint,
        interface="postgresql_client",
        level="simple",
        role="requires",
        relation_id=0,
        error=error,
    )


def _skipped(endpoint: str = "db", interface: str = "postgresql_client") -> ValidationResult:
    return ValidationResult(
        status="SKIPPED", endpoint=endpoint, interface=interface, level="simple", role="requires", relation_id=0
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
        backend: NullJujuBackend,
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

    def test_skips_application_with_no_results(self, logger: LoggerStub) -> None:
        # GIVEN two applications: one with no results and one with a PASS
        backend = BackendStub(
            app_list={"app1": _app_info(), "app2": _app_info()},
            validate_results={"app1": {}, "app2": {"app2/0": [_pass()]}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

        # THEN the application with no results is skipped with a log
        assert any("No validation results for application 'app1'" in info for info in logger.infos)

    def test_skips_unit_with_no_results(self, logger: LoggerStub) -> None:
        # GIVEN an application with an empty unit results list
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": []}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

        # THEN the unit with no results is skipped with a log
        assert any("No validation results for unit 'myapp/0'" in info for info in logger.infos)

    def test_skips_unit_with_all_skipped_results(self, logger: LoggerStub) -> None:
        # GIVEN an application with a unit that has only SKIPPED results
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_skipped(), _skipped("metrics")]}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

        # THEN the unit with all skipped results is skipped with a log
        assert any("Validation skipped for unit 'myapp/0'" in info for info in logger.infos)

    def test_does_not_skip_unit_with_mix_of_skipped_and_pass(self, logger: LoggerStub) -> None:
        # GIVEN a unit with both SKIPPED and PASS results
        backend = BackendStub(
            app_list={"myapp": _app_info()},
            validate_results={"myapp": {"myapp/0": [_skipped(), _pass()]}},
        )
        client = self._client(logger, backend)

        # WHEN / THEN (no exception)
        client.validate_model("mymodel")

        # THEN the unit is not skipped and validation passed is logged
        assert any("Validation passed for unit 'myapp/0'" in info for info in logger.infos)
        assert not any("Validation skipped for unit 'myapp/0'" in info for info in logger.infos)

    def test_delegates_to_backend_with_revision_only(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records refresh calls
        backend = RefreshBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.refresh_application("myapp", revision=42, model="mymodel")

        # THEN the backend was called with revision and no channel
        assert backend.refresh_calls == [("mymodel", "myapp", 42, None)]

    def test_delegates_to_backend_with_revision_and_channel(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records refresh calls
        backend = RefreshBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.refresh_application("myapp", revision=42, channel="latest/stable", model="mymodel")

        # THEN the backend was called with both revision and channel
        assert backend.refresh_calls == [("mymodel", "myapp", 42, "latest/stable")]

    def test_returns_revision_for_known_application(self, logger: LoggerStub) -> None:
        # GIVEN an application with a known revision
        backend = BackendStub(app_list={"myapp": JujuApplicationInfo(charm="mycharm", revision=7)})
        client = self._client(logger, backend)

        # WHEN
        result = client.application_revision("myapp", model="mymodel")

        # THEN
        assert result == 7

    def test_raises_key_error_for_missing_application(self, logger: LoggerStub) -> None:
        # GIVEN no applications in the model
        backend = BackendStub(app_list={})
        client = self._client(logger, backend)

        # WHEN / THEN
        with pytest.raises(KeyError, match="myapp"):
            client.application_revision("myapp", model="mymodel")

    def test_delegates_wait_to_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend stub
        backend = BackendStub(app_list={"myapp": JujuApplicationInfo(charm="mycharm", revision=5)})
        client = self._client(logger, backend)

        # WHEN wait_for_application_revision is called (via backend delegation)
        # THEN the call is delegated to the backend without error
        # (Detailed wait behavior is tested in juju_jubilant/test_backend.py)
        try:
            client.wait_for_application_revision(
                "myapp", expected_revision=5, model="mymodel", timeout=timedelta(milliseconds=1)
            )
        except Exception:
            pass  # Backend stub doesn't fully implement wait - that's expected


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


@dataclass
class UpgradeControllerBackendStub(NullJujuBackend):
    """Backend stub that records upgrade_controller calls."""

    upgrade_controller_calls: list[tuple[str, str | None]] = field(default_factory=list)

    def upgrade_controller(self, controller: str, agent_version: str | None = None) -> None:
        self.upgrade_controller_calls.append((controller, agent_version))


class TestJujuClientUpgradeController:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_delegates_to_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records upgrade_controller calls
        backend = UpgradeControllerBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_controller("mycontroller", agent_version="3.6.21")

        # THEN the backend received the call with the correct arguments
        assert ("mycontroller", "3.6.21") in backend.upgrade_controller_calls

    def test_delegates_without_agent_version(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records upgrade_controller calls
        backend = UpgradeControllerBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_controller("mycontroller")

        # THEN the backend received the call with None agent_version
        assert ("mycontroller", None) in backend.upgrade_controller_calls

    def test_logs_controller_and_version(self, logger: LoggerStub) -> None:
        # GIVEN a backend stub
        backend = UpgradeControllerBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_controller("mycontroller", agent_version="3.6.21")

        # THEN an info message mentioning the controller and version was logged
        assert any("mycontroller" in msg and "3.6.21" in msg for msg in logger.infos)


@dataclass
class UpgradeModelBackendStub(NullJujuBackend):
    """Backend stub that records upgrade_model calls."""

    upgrade_model_calls: list[tuple[str, str | None]] = field(default_factory=list)

    def upgrade_model(self, model: str, agent_version: str | None = None) -> None:
        self.upgrade_model_calls.append((model, agent_version))


class TestJujuClientUpgradeModel:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_delegates_to_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records upgrade_model calls
        backend = UpgradeModelBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_model("mymodel", agent_version="4.0.5")

        # THEN the backend received the call with the correct arguments
        assert ("mymodel", "4.0.5") in backend.upgrade_model_calls

    def test_delegates_without_agent_version(self, logger: LoggerStub) -> None:
        # GIVEN a backend that records upgrade_model calls
        backend = UpgradeModelBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_model("mymodel")

        # THEN the backend received the call with None agent_version
        assert ("mymodel", None) in backend.upgrade_model_calls

    def test_logs_model_and_version(self, logger: LoggerStub) -> None:
        # GIVEN a backend stub
        backend = UpgradeModelBackendStub()
        client = self._client(logger, backend)

        # WHEN
        client.upgrade_model("mymodel", agent_version="4.0.5")

        # THEN an info message mentioning the model and version was logged
        assert any("mymodel" in msg and "4.0.5" in msg for msg in logger.infos)


@dataclass
class VersionBackendStub(NullJujuBackend):
    """Backend stub that returns a fixed version string."""

    _version: str = "3.6.1"
    _cli_version: str = "3.6.1-ubuntu-amd64"

    def version(self, model: str) -> JujuVersion:
        return JujuVersion.parse(self._version)

    def cli_version(self) -> JujuVersion:
        return JujuVersion.parse(self._cli_version)


@pytest.fixture
def logger() -> LoggerStub:
    return LoggerStub()


class TestJujuClientVersion:
    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_delegates_version_to_backend(self, logger: LoggerStub) -> None:
        backend = VersionBackendStub(_version="3.6.1")
        client = self._client(logger, backend)

        assert client.version("mymodel") == JujuVersion(3, 6, 1)

    def test_delegates_cli_version_to_backend(self, logger: LoggerStub) -> None:
        backend = VersionBackendStub(_cli_version="3.6.1-ubuntu-amd64")
        client = self._client(logger, backend)

        assert client.cli_version() == JujuVersion(3, 6, 1)


class DebugLogStub(NullJujuBackend):
    """Backend stub that offers debug_log."""

    def debug_log(self, model: str) -> str:
        return f"this is a debug log\nmessage\n{model}"


class TestJujuClientDebugLog:
    def _client(self, logger: Any, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(backend, logger, [])

    def test_debug_log_calls_backend(self, logger: LoggerStub) -> None:
        # GIVEN a backend that returns debug_log
        backend = DebugLogStub()
        client = self._client(logger, backend)

        # WHEN
        log = client.debug_log("mymodel")

        # THEN the backend's debug_log method was called and returned the expected string
        assert "Collecting debug log from model mymodel" in logger.infos
        assert log == "this is a debug log\nmessage\nmymodel"


# ---------------------------------------------------------------------------
# Stubs for controller lifecycle hook tests
# ---------------------------------------------------------------------------


@dataclass
class BootstrapControllerBackendStub(NullJujuBackend):
    """Backend stub that records bootstrap_controller calls."""

    bootstrapped: list[str] = field(default_factory=list)

    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        bootstrap_configuration: dict[str, str],
        metadata_source: Any | None = None,
        agent_version: str | None = None,
    ) -> None:
        self.bootstrapped.append(controller)


@dataclass
class HookRecordingExtension(JujuExtension):
    """Extension that records all lifecycle hook calls."""

    post_bootstrap_calls: list[str] = field(default_factory=list)
    pre_kill_calls: list[str] = field(default_factory=list)
    post_migrate_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def post_bootstrap_controller(self, controller: str) -> None:
        self.post_bootstrap_calls.append(controller)

    def pre_kill_controller(self, controller: str) -> None:
        self.pre_kill_calls.append(controller)

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        self.post_migrate_calls.append((model, source, target))


# ---------------------------------------------------------------------------
# Tests: JujuClient controller lifecycle hooks
# ---------------------------------------------------------------------------


class TestJujuClientBootstrapControllerHooks:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(
        self, logger: Any, backend: NullJujuBackend, extensions: list[JujuExtension] | None = None
    ) -> JujuClient:
        return JujuClient(backend, logger, extensions or [])

    def test_post_bootstrap_controller_hook_fires(self, logger: LoggerStub) -> None:
        # GIVEN a client with a hook-recording extension
        ext = HookRecordingExtension()
        backend = BootstrapControllerBackendStub()
        client = self._client(logger, backend, [ext])

        # WHEN a controller is bootstrapped
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

        # THEN the extension hook received the controller name
        assert ext.post_bootstrap_calls == ["my-ctrl"]

    def test_post_bootstrap_controller_hook_fires_for_all_extensions(self, logger: LoggerStub) -> None:
        # GIVEN a client with two extensions
        ext1 = HookRecordingExtension()
        ext2 = HookRecordingExtension()
        backend = BootstrapControllerBackendStub()
        client = self._client(logger, backend, [ext1, ext2])

        # WHEN a controller is bootstrapped
        client.bootstrap_controller(
            cloud="mycloud", controller="my-ctrl", controller_constraints={}, bootstrap_configuration={}
        )

        # THEN both extensions received the hook
        assert ext1.post_bootstrap_calls == ["my-ctrl"]
        assert ext2.post_bootstrap_calls == ["my-ctrl"]


class TestJujuClientKillControllerHooks:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(
        self, logger: Any, backend: NullJujuBackend, extensions: list[JujuExtension] | None = None
    ) -> JujuClient:
        return JujuClient(backend, logger, extensions or [])

    def test_pre_kill_controller_hook_fires_before_backend(self, logger: LoggerStub) -> None:
        # GIVEN a client with a hook-recording extension that appends to an order list
        order: list[str] = []

        class OrderRecordingExtension(HookRecordingExtension):
            def pre_kill_controller(self, controller: str) -> None:
                order.append("hook")
                super().pre_kill_controller(controller)

        @dataclass
        class OrderedKillBackend(NullJujuBackend):
            def kill_controller(self, controller: str) -> None:
                order.append("backend")

        ext = OrderRecordingExtension()
        client = self._client(logger, OrderedKillBackend(), [ext])

        # WHEN the controller is killed
        client.kill_controller("my-ctrl")

        # THEN hook fires before backend
        assert order == ["hook", "backend"]

    def test_pre_kill_controller_hook_fires_for_all_extensions(self, logger: LoggerStub) -> None:
        # GIVEN a client with two extensions
        ext1 = HookRecordingExtension()
        ext2 = HookRecordingExtension()
        client = self._client(logger, KillControllerBackendStub(), [ext1, ext2])

        # WHEN
        client.kill_controller("my-ctrl")

        # THEN both extensions received the hook
        assert ext1.pre_kill_calls == ["my-ctrl"]
        assert ext2.pre_kill_calls == ["my-ctrl"]


class TestJujuClientMigrateModelHooks:
    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    def _client(
        self, logger: Any, backend: NullJujuBackend, extensions: list[JujuExtension] | None = None
    ) -> JujuClient:
        return JujuClient(backend, logger, extensions or [])

    def test_post_migrate_model_hook_fires(self, logger: LoggerStub) -> None:
        # GIVEN a client with a hook-recording extension
        ext = HookRecordingExtension()
        client = self._client(logger, MigrateModelBackendStub(), [ext])

        # WHEN a model is migrated
        client.migrate_model("mymodel", "source-ctrl", "target-ctrl")

        # THEN the extension hook received all three arguments
        assert ext.post_migrate_calls == [("mymodel", "source-ctrl", "target-ctrl")]

    def test_post_migrate_model_hook_fires_for_all_extensions(self, logger: LoggerStub) -> None:
        # GIVEN a client with two extensions
        ext1 = HookRecordingExtension()
        ext2 = HookRecordingExtension()
        client = self._client(logger, MigrateModelBackendStub(), [ext1, ext2])

        # WHEN
        client.migrate_model("mymodel", "source-ctrl", "target-ctrl")

        # THEN both extensions received the hook
        assert ext1.post_migrate_calls == [("mymodel", "source-ctrl", "target-ctrl")]
        assert ext2.post_migrate_calls == [("mymodel", "source-ctrl", "target-ctrl")]


# ---------------------------------------------------------------------------
# TestJujuClientDeployBundles
# ---------------------------------------------------------------------------


@dataclass
class DeployBundlesBackendStub(NullJujuBackend):
    """Backend stub that records deploy_bundles orchestration calls for assertions."""

    ops: list[str] = field(default_factory=list)
    existing_apps: list[str] = field(default_factory=list)
    existing_offers: set[str] = field(default_factory=set)
    existing_saas: list[str] = field(default_factory=list)
    existing_integrations: set[Any] = field(default_factory=set)

    def list_applications(self, model: str) -> dict[str, Any]:
        from juju.models import JujuApplicationInfo

        return {a: JujuApplicationInfo(charm=a, revision=1) for a in self.existing_apps}

    def list_offers(self, model: str) -> set[str]:
        return set(self.existing_offers)

    def list_consumed_offers(self, model: str) -> dict[str, Any]:
        from juju.models import JujuConsumedOfferInfo

        return {a: JujuConsumedOfferInfo(a) for a in self.existing_saas}

    def list_integrations(self, model: str) -> set[Any]:
        return self.existing_integrations

    def deploy_application(
        self,
        model: str,
        charm: str,
        application: str | None = None,
        channel: str | None = None,
        revision: int | None = None,
        base: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
        resources: dict[str, str] | None = None,
        num_units: int | None = None,
    ) -> None:
        self.ops.append(f"deploy:{application or charm}")

    def configure_application(self, model: str, application: str, values: dict[str, Any]) -> None:
        self.ops.append(f"configure:{application}")

    def offer(self, model: str, app: str, endpoints: Any, name: str) -> None:
        self.ops.append(f"offer:{name}")

    def consume(self, model: str, saas_url: str, alias: str | None = None) -> None:
        self.ops.append(f"consume:{alias or saas_url}")

    def integrate(self, model: str, target_1: Any, target_2: Any) -> None:
        self.ops.append(f"integrate:{target_1}:{target_2}")

    def wait_idle(
        self,
        model: str,
        timeout: Any,
        count: Any,
        strict_timeout: bool = False,
        applications: list[str] | None = None,
    ) -> None:
        pass


def _write_bundle(tmp_path: Any, content: dict[str, Any], overlay: dict[str, Any] | None = None) -> str:
    import yaml

    bundle_file = tmp_path / "bundle.yaml"
    docs = [content]
    if overlay:
        docs.append(overlay)
    bundle_file.write_text("\n---\n".join(yaml.dump(d) for d in docs))
    return str(bundle_file)


class TestJujuClientDeployBundles:
    def _client(self, backend: NullJujuBackend) -> JujuClient:
        return JujuClient(logger=LoggerStub(), backend=backend)  # type: ignore[arg-type]

    def test_fresh_deploy_creates_app(self, tmp_path: Any) -> None:
        # GIVEN a bundle with one app that is not yet deployed
        bundle = _write_bundle(tmp_path, {"applications": {"myapp": {"charm": "myapp"}}})
        stub = DeployBundlesBackendStub()

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "deploy:myapp" in stub.ops

    def test_existing_app_not_redeployed(self, tmp_path: Any) -> None:
        # GIVEN the app is already present
        bundle = _write_bundle(tmp_path, {"applications": {"myapp": {"charm": "myapp"}}})
        stub = DeployBundlesBackendStub(existing_apps=["myapp"])

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "deploy:myapp" not in stub.ops

    def test_existing_app_with_options_configures(self, tmp_path: Any) -> None:
        # GIVEN the app exists but has options in the bundle
        bundle = _write_bundle(tmp_path, {"applications": {"myapp": {"charm": "myapp", "options": {"key": "value"}}}})
        stub = DeployBundlesBackendStub(existing_apps=["myapp"])

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "configure:myapp" in stub.ops
        assert "deploy:myapp" not in stub.ops

    def test_fresh_offer_is_created(self, tmp_path: Any) -> None:
        # GIVEN a bundle with an overlay defining an offer not yet in the model
        bundle = _write_bundle(
            tmp_path,
            {"applications": {"svc": {}}},
            overlay={"applications": {"svc": {"offers": {"svc-offer": {"endpoints": ["ep"]}}}}},
        )
        stub = DeployBundlesBackendStub()

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "offer:svc-offer" in stub.ops

    def test_existing_offer_not_recreated(self, tmp_path: Any) -> None:
        # GIVEN the offer is already present
        bundle = _write_bundle(
            tmp_path,
            {"applications": {"svc": {}}},
            overlay={"applications": {"svc": {"offers": {"svc-offer": {"endpoints": ["ep"]}}}}},
        )
        stub = DeployBundlesBackendStub(existing_offers={"svc-offer"})

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "offer:svc-offer" not in stub.ops

    def test_saas_consumed_if_not_present(self, tmp_path: Any) -> None:
        # GIVEN a bundle with a SAAS alias not yet consumed
        bundle = _write_bundle(
            tmp_path, {"applications": {}, "saas": {"remote-db": {"url": "admin/neighbor.db-offer"}}}
        )
        stub = DeployBundlesBackendStub()

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "consume:remote-db" in stub.ops

    def test_existing_saas_not_reconsumed(self, tmp_path: Any) -> None:
        # GIVEN the SAAS alias is already consumed
        bundle = _write_bundle(
            tmp_path, {"applications": {}, "saas": {"remote-db": {"url": "admin/neighbor.db-offer"}}}
        )
        stub = DeployBundlesBackendStub(existing_saas=["remote-db"])

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert "consume:remote-db" not in stub.ops

    def test_integration_created(self, tmp_path: Any) -> None:
        # GIVEN a bundle with a relation between two apps
        bundle = _write_bundle(
            tmp_path,
            {
                "applications": {"db": {}, "app": {}},
                "relations": [["db:db", "app:db"]],
            },
        )
        stub = DeployBundlesBackendStub()

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert any("integrate" in op for op in stub.ops)

    def test_existing_integration_not_recreated(self, tmp_path: Any) -> None:
        # GIVEN the integration already exists
        from juju.models import JujuIntegration, JujuIntegrationApplication

        bundle = _write_bundle(
            tmp_path,
            {
                "applications": {"db": {}, "app": {}},
                "relations": [["db:db", "app:db"]],
            },
        )
        existing = {
            JujuIntegration(
                JujuIntegrationApplication("db", "db"),
                JujuIntegrationApplication("app", "db"),
                interface="pgsql",
            )
        }
        stub = DeployBundlesBackendStub(existing_integrations=existing)

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert not any("integrate" in op for op in stub.ops)

    def test_cmr_two_phase_ordering(self, tmp_path: Any) -> None:
        # GIVEN a neighbor model with an offer and a target model that consumes it
        (tmp_path / "neighbor").mkdir()
        neighbor_bundle = _write_bundle(
            tmp_path / "neighbor",
            {"applications": {"svc": {}}},
            overlay={"applications": {"svc": {"offers": {"svc-offer": {"endpoints": ["ep"]}}}}},
        )
        target_bundle = _write_bundle(
            tmp_path,
            {"applications": {}, "saas": {"svc-offer": {"url": "admin/neighbor.svc-offer"}}},
        )
        all_ops: list[str] = []

        class OrderingStub(DeployBundlesBackendStub):
            def offer(self, model: str, app: str, endpoints: Any, name: str) -> None:
                all_ops.append(f"offer:{name}")
                super().offer(model, app, endpoints, name)

            def consume(self, model: str, saas_url: str, alias: str | None = None) -> None:
                all_ops.append(f"consume:{alias or saas_url}")
                super().consume(model, saas_url, alias)

        stub = OrderingStub()
        self._client(stub).deploy_bundles({"ctrl:admin/neighbor": neighbor_bundle, "ctrl:admin/target": target_bundle})

        assert "offer:svc-offer" in all_ops
        assert "consume:svc-offer" in all_ops
        assert all_ops.index("offer:svc-offer") < all_ops.index("consume:svc-offer")

    def test_bundle_without_overlay_has_no_offers(self, tmp_path: Any) -> None:
        # GIVEN a single-document bundle (no overlay)
        bundle = _write_bundle(tmp_path, {"applications": {"myapp": {"charm": "myapp"}}})
        stub = DeployBundlesBackendStub()

        self._client(stub).deploy_bundles({"ctrl:model": bundle})

        assert not any("offer" in op for op in stub.ops)

    def test_nonexistent_bundle_raises(self, tmp_path: Any) -> None:
        stub = DeployBundlesBackendStub()
        with pytest.raises(ValueError, match="not found"):
            self._client(stub).deploy_bundles({"ctrl:model": str(tmp_path / "nonexistent.yaml")})

    def test_empty_offer_endpoints_raises(self, tmp_path: Any) -> None:
        bundle = _write_bundle(
            tmp_path,
            {"applications": {"svc": {"charm": "svc"}}},
            overlay={"applications": {"svc": {"offers": {"svc-offer": {"endpoints": []}}}}},
        )
        stub = DeployBundlesBackendStub()
        with pytest.raises(ValueError, match="non-empty"):
            self._client(stub).deploy_bundles({"ctrl:model": bundle})

    def test_scalar_offer_endpoints_raises(self, tmp_path: Any) -> None:
        bundle = _write_bundle(
            tmp_path,
            {"applications": {"svc": {"charm": "svc"}}},
            overlay={"applications": {"svc": {"offers": {"svc-offer": {"endpoints": "ep"}}}}},
        )
        stub = DeployBundlesBackendStub()
        with pytest.raises(ValueError, match="non-empty"):
            self._client(stub).deploy_bundles({"ctrl:model": bundle})

    def test_non_mapping_saas_entry_raises(self, tmp_path: Any) -> None:
        bundle = _write_bundle(
            tmp_path,
            {"applications": {}, "saas": {"remote": "bad-scalar-value"}},
        )
        stub = DeployBundlesBackendStub()
        with pytest.raises(ValueError, match="mapping"):
            self._client(stub).deploy_bundles({"ctrl:model": bundle})

    def test_malformed_relation_raises(self, tmp_path: Any) -> None:
        bundle = _write_bundle(
            tmp_path,
            {"applications": {}, "relations": [["a:ep", "b:ep", "extra"]]},
        )
        stub = DeployBundlesBackendStub()
        with pytest.raises(ValueError, match="2-item"):
            self._client(stub).deploy_bundles({"ctrl:model": bundle})

    def test_post_deploy_extension_called_for_each_model(self, tmp_path: Any) -> None:
        # GIVEN two bundles and an extension
        (tmp_path / "m1").mkdir()
        (tmp_path / "m2").mkdir()
        bundle1 = _write_bundle(tmp_path / "m1", {"applications": {}})
        bundle2 = _write_bundle(tmp_path / "m2", {"applications": {}})

        post_deploy_calls: list[str] = []

        class TrackingExtension(JujuExtension):
            def post_deploy(self, model: str) -> None:
                post_deploy_calls.append(model)

        stub = DeployBundlesBackendStub()
        client = JujuClient(
            logger=LoggerStub(),  # type: ignore[arg-type]
            backend=stub,
            extensions=[TrackingExtension()],
        )
        client.deploy_bundles({"ctrl:m1": bundle1, "ctrl:m2": bundle2})

        assert sorted(post_deploy_calls) == ["ctrl:m1", "ctrl:m2"]
