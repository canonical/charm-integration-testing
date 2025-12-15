# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field

import pytest
from extensions.configure_livepatch_server.extensions import (
    LIVEPATCH_SERVER_CHARM,
    LIVEPATCH_SERVER_CONFIGURE_MESSAGE,
    ConfigureLivepatchServerExtension,
)


@dataclass
class JujuStub:
    applications: dict[str, str] = field(default_factory=lambda: {"livepatch": LIVEPATCH_SERVER_CHARM})
    waited_scaled: list[tuple[str, str, str]] = field(default_factory=list)
    waited_settled: list[tuple[str, str, str]] = field(default_factory=list)
    waited_messages: list[tuple[str, str, str, str]] = field(default_factory=list)
    actions: list[tuple[str, str, str, dict[str, str]]] = field(default_factory=list)
    configured: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    unit_ips: dict[str, str] = field(default_factory=lambda: {"livepatch/leader": "10.1.2.157"})

    def list_applications(self, model: str):
        return self.applications.keys()

    def application_charm(self, model: str, application: str):
        return self.applications[application]

    def wait_application_scaled(self, model: str, application: str, timeout):
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout):
        self.waited_settled.append((model, application, str(timeout)))

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout):
        self.waited_messages.append((model, unit, message, str(timeout)))

    def run_action(self, model: str, unit: str, action: str, params: dict[str, str]):
        self.actions.append((model, unit, action, params))

    def configure_application(self, model: str, application: str, values: dict[str, str]):
        self.configured.append((model, application, values))

    def unit_ip(self, model: str, unit: str):
        return self.unit_ips[unit]


class LoggerStub:
    def __init__(self):
        self.messages: dict[str, list[str]] = {"info": [], "warning": []}

    def info(self, message: str):
        self.messages["info"].append(message)

    def warning(self, message: str):
        self.messages["warning"].append(message)


class TestConfigureLivepatchServerExtension:
    @pytest.fixture
    def juju(self):
        return JujuStub()

    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    @pytest.fixture
    def extension_with_token(self, juju, logger):
        return ConfigureLivepatchServerExtension(juju, logger, "test-token-123")

    @pytest.fixture
    def extension_without_token(self, juju, logger):
        return ConfigureLivepatchServerExtension(juju, logger, None)

    class TestPostDeploy:
        def test_configures_livepatch_server_when_present(self, extension_with_token, juju):
            # GIVEN a model with a livepatch server application
            # WHEN post_deploy is called
            extension_with_token.post_deploy("test-model")

            # THEN the livepatch server is configured
            assert len(juju.waited_scaled) > 0
            assert len(juju.actions) > 0
            assert len(juju.configured) > 0

        def test_ignores_non_livepatch_apps(self, extension_with_token):
            # GIVEN a model with no livepatch server applications
            juju_stub = JujuStub(applications={"other-app": "other-charm"})
            extension = ConfigureLivepatchServerExtension(juju_stub, logging.getLogger("test"), "test-token")  # type: ignore[arg-type]

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no configuration happens
            assert juju_stub.waited_scaled == []
            assert juju_stub.actions == []
            assert juju_stub.configured == []

        def test_skips_configuration_without_token(self, extension_without_token, juju, logger):
            # GIVEN an extension without a token
            # WHEN post_deploy is called
            extension_without_token.post_deploy("test-model")

            # THEN a warning is logged and no configuration happens
            assert any("No Ubuntu Pro token provided" in msg for msg in logger.messages["warning"])
            assert juju.waited_scaled == []
            assert juju.actions == []
            assert juju.configured == []

    class TestConfigureLivepatchServer:
        def test_complete_configuration_flow(self, extension_with_token, juju):
            # GIVEN a ready extension with a token
            # WHEN configure_livepatch_server is called
            extension_with_token.configure_livepatch_server("test-model", "livepatch")

            # THEN the full configuration workflow executes
            # Wait for scaling
            assert ("test-model", "livepatch", "0:10:00") in juju.waited_scaled

            # Wait for settling
            assert ("test-model", "livepatch", "0:10:00") in juju.waited_settled

            # Wait for configure message
            assert (
                "test-model",
                "livepatch/leader",
                LIVEPATCH_SERVER_CONFIGURE_MESSAGE,
                "0:10:00",
            ) in juju.waited_messages

            # Run the get-resource-token action
            assert (
                "test-model",
                "livepatch/leader",
                "get-resource-token",
                {"contract-token": "test-token-123"},
            ) in juju.actions

            # Configure the server URL template
            assert (
                "test-model",
                "livepatch",
                {"server.url-template": "http://10.1.2.157:8080/v1/patches/{filename}"},
            ) in juju.configured

        def test_skips_when_no_token(self, extension_without_token, juju, logger):
            # GIVEN an extension without a token
            # WHEN configure_livepatch_server is called
            extension_without_token.configure_livepatch_server("test-model", "livepatch")

            # THEN a warning is logged and workflow is skipped
            assert any("No Ubuntu Pro token provided" in msg for msg in logger.messages["warning"])
            assert juju.waited_scaled == []
            assert juju.actions == []
            assert juju.configured == []

        def test_uses_correct_unit_ip(self, juju, logger):
            # GIVEN an extension with a specific unit IP
            juju.unit_ips = {"livepatch/leader": "192.168.1.100"}
            extension = ConfigureLivepatchServerExtension(juju, logger, "test-token")

            # WHEN configure_livepatch_server is called
            extension.configure_livepatch_server("test-model", "livepatch")

            # THEN the URL template uses the correct IP
            assert (
                "test-model",
                "livepatch",
                {"server.url-template": "http://192.168.1.100:8080/v1/patches/{filename}"},
            ) in juju.configured

    class TestInitialization:
        def test_stores_token(self, juju, logger):
            # GIVEN a token
            token = "my-ubuntu-pro-token"

            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, token)

            # THEN the token is stored
            assert extension.ubuntu_pro_token == token

        def test_stores_none_token(self, juju, logger):
            # GIVEN no token
            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, None)

            # THEN None is stored
            assert extension.ubuntu_pro_token is None

        def test_stores_juju_and_logger(self, juju, logger):
            # GIVEN juju and logger instances
            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, "token")

            # THEN they are stored
            assert extension.juju is juju
            assert extension.logger is logger
