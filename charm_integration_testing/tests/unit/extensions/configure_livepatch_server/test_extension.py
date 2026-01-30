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
from juju.backend import JujuBackend

from ..shared import JujuStub as JujuStubBase


@dataclass
class JujuStub(JujuStubBase):
    applications: dict[str, str] = field(default_factory=lambda: {"livepatch": LIVEPATCH_SERVER_CHARM})
    unit_ips: dict[str, str] = field(default_factory=lambda: {"livepatch/leader": "10.1.2.157"})

    def num_units(self) -> None:  # type: ignore[override]
        pass

    def list_integrations(self) -> None:  # type: ignore[override]
        pass

    def integration_exists(self) -> None:  # type: ignore[override]
        pass

    def wait_idle(self) -> None:  # type: ignore[override]
        pass

    def juju_status_text(self) -> None:  # type: ignore[override]
        pass

    def integrate(self) -> None:  # type: ignore[override]
        pass

    def remove_integration(self) -> None:  # type: ignore[override]
        pass

    def deploy_bundle_file(self) -> None:  # type: ignore[override]
        pass

    def remove_applications(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_integration(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_units(self) -> None:  # type: ignore[override]
        pass

    def application_units(self) -> None:  # type: ignore[override]
        pass

    def exec_unit(self) -> None:  # type: ignore[override]
        pass

    def add_secret(self) -> None:  # type: ignore[override]
        pass

    def read_secret(self) -> None:  # type: ignore[override]
        pass

    def grant_secret(self) -> None:  # type: ignore[override]
        pass

    def remove_secret(self) -> None:  # type: ignore[override]
        pass

    def deploy_application(self) -> None:  # type: ignore[override]
        pass

    def scp(self) -> None:  # type: ignore[override]
        pass

    def ssh(self) -> None:  # type: ignore[override]
        pass

    def version(self) -> None:  # type: ignore[override]
        pass


class LoggerStub(logging.Logger):
    def __init__(self) -> None:
        self.messages: dict[str, list[str]] = {"info": [], "warning": []}

    def info(self, message: str) -> None:  # type: ignore[override]
        self.messages["info"].append(message)

    def warning(self, message: str) -> None:  # type: ignore[override]
        self.messages["warning"].append(message)


class TestConfigureLivepatchServerExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        return JujuStub()

    @pytest.fixture
    def logger(self) -> LoggerStub:
        return LoggerStub()

    @pytest.fixture
    def extension_with_token(self, juju: JujuBackend, logger: logging.Logger) -> ConfigureLivepatchServerExtension:
        return ConfigureLivepatchServerExtension(juju, logger, "test-token-123")

    @pytest.fixture
    def extension_without_token(self, juju: JujuBackend, logger: logging.Logger) -> ConfigureLivepatchServerExtension:
        return ConfigureLivepatchServerExtension(juju, logger, None)

    class TestPostDeploy:
        def test_configures_livepatch_server_when_present(
            self, extension_with_token: ConfigureLivepatchServerExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with a livepatch server application
            # WHEN post_deploy is called
            extension_with_token.post_deploy("test-model")

            # THEN the livepatch server is configured
            assert len(juju.waited_scaled) > 0
            assert len(juju.actions) > 0
            assert len(juju.configured_applications) > 0

        def test_ignores_non_livepatch_apps(self, extension_with_token: ConfigureLivepatchServerExtension) -> None:
            # GIVEN a model with no livepatch server applications
            juju_stub = JujuStub(applications={"other-app": "other-charm"})
            extension = ConfigureLivepatchServerExtension(juju_stub, logging.getLogger("test"), "test-token")

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no configuration happens
            assert juju_stub.waited_scaled == []
            assert juju_stub.actions == []
            assert juju_stub.configured_applications == []

        def test_skips_configuration_without_token(
            self, extension_without_token: ConfigureLivepatchServerExtension, juju: JujuStub, logger: LoggerStub
        ) -> None:
            # GIVEN an extension without a token
            # WHEN post_deploy is called
            extension_without_token.post_deploy("test-model")

            # THEN a warning is logged and no configuration happens
            assert any("No Ubuntu Pro token provided" in msg for msg in logger.messages["warning"])
            assert juju.waited_scaled == []
            assert juju.actions == []
            assert juju.configured_applications == []

    class TestConfigureLivepatchServer:
        def test_complete_configuration_flow(
            self, extension_with_token: ConfigureLivepatchServerExtension, juju: JujuStub
        ) -> None:
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
            ) in juju.configured_applications

        def test_skips_when_no_token(
            self, extension_without_token: ConfigureLivepatchServerExtension, juju: JujuStub, logger: LoggerStub
        ) -> None:
            # GIVEN an extension without a token
            # WHEN configure_livepatch_server is called
            extension_without_token.configure_livepatch_server("test-model", "livepatch")

            # THEN a warning is logged and workflow is skipped
            assert any("No Ubuntu Pro token provided" in msg for msg in logger.messages["warning"])
            assert juju.waited_scaled == []
            assert juju.actions == []
            assert juju.configured_applications == []

        def test_uses_correct_unit_ip(self, juju: JujuStub, logger: LoggerStub) -> None:
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
            ) in juju.configured_applications

    class TestInitialization:
        def test_stores_token(self, juju: JujuStub, logger: LoggerStub) -> None:
            # GIVEN a token
            token = "my-ubuntu-pro-token"

            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, token)

            # THEN the token is stored
            assert extension.ubuntu_pro_token == token

        def test_stores_none_token(self, juju: JujuStub, logger: LoggerStub) -> None:
            # GIVEN no token
            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, None)

            # THEN None is stored
            assert extension.ubuntu_pro_token is None

        def test_stores_juju_and_logger(self, juju: JujuStub, logger: LoggerStub) -> None:
            # GIVEN juju and logger instances
            # WHEN creating the extension
            extension = ConfigureLivepatchServerExtension(juju, logger, "token")

            # THEN they are stored
            assert extension.juju is juju
            assert extension.logger is logger
