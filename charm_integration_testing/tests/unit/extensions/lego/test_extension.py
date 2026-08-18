# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from dataclasses import dataclass, field

import pytest
from extensions.lego.extension import (
    DUMMY_HTTPREQ_ENDPOINT,
    HTTPREQ_ENDPOINT_CONFIG_KEY,
    LEGO_CHARM,
    LegoExtension,
)
from juju.backend import JujuBackend
from juju.resource_registry.handles import JujuModelHandle

from ..shared import JujuStub as JujuStubBase

TEST_MODEL: JujuModelHandle = JujuModelHandle(controller="test-controller", model="test-model")


@dataclass
class JujuStub(JujuStubBase):
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    secrets_granted: list[tuple[str, str]] = field(default_factory=list)
    secrets_removed: list[str] = field(default_factory=list)
    next_secret_id: str = "new-secret-id"

    def add_secret(self, model: JujuModelHandle | str, name: str, values: dict[str, str]) -> str:
        self.secrets[name] = values
        return self.next_secret_id

    def grant_secret(self, model: JujuModelHandle | str, name_or_id: str, application: str) -> None:
        self.secrets_granted.append((name_or_id, application))

    def remove_secret(self, model: JujuModelHandle | str, name_or_id: str) -> None:
        self.secrets_removed.append(name_or_id)
        try:
            del self.secrets[name_or_id]
        except KeyError as err:
            raise subprocess.CalledProcessError(-1, ["remove-secret", name_or_id], stderr="not found") from err


class TestLegoExtension:
    @pytest.fixture
    def logger(self) -> logging.Logger:
        return logging.getLogger("test")

    @pytest.fixture
    def juju(self) -> JujuStub:
        return JujuStub(applications={"lego": LEGO_CHARM})

    @pytest.fixture
    def extension(self, juju: JujuBackend, logger: logging.Logger) -> LegoExtension:
        return LegoExtension(juju, logger)

    class TestPostDeploy:
        def test_configures_lego_when_present(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN a model with a lego application
            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN the lego application is configured with the httpreq plugin and a secret
            assert len(juju.configured_applications) == 1
            model, application, values = juju.configured_applications[0]
            assert model == TEST_MODEL.uri
            assert application == "lego"
            assert values["plugin"] == "httpreq"
            assert values["plugin-config-secret-id"] == f"secret:{juju.next_secret_id}"

        def test_ignores_non_lego_apps(self, logger: logging.Logger) -> None:
            # GIVEN a model with no lego applications
            juju_stub = JujuStub(applications={"other-app": "other-charm"})
            extension = LegoExtension(juju_stub, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN no configuration happens
            assert juju_stub.configured_applications == []
            assert juju_stub.secrets == {}

        def test_configures_multiple_lego_applications(self, logger: logging.Logger) -> None:
            # GIVEN a model with two lego applications
            juju_stub = JujuStub(applications={"lego-a": LEGO_CHARM, "lego-b": LEGO_CHARM})
            extension = LegoExtension(juju_stub, logger)

            # WHEN post_deploy is called
            extension.post_deploy(TEST_MODEL)

            # THEN both applications are configured independently
            configured_apps = {application for _, application, _ in juju_stub.configured_applications}
            assert configured_apps == {"lego-a", "lego-b"}

    class TestConfigureLego:
        def test_creates_and_grants_secret_with_dummy_endpoint(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN a lego application with no existing configuration
            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN a secret carrying the dummy httpreq endpoint is created and granted
            secret_name = extension.lego_plugin_config_secret_name("lego")
            assert juju.secrets[secret_name] == {HTTPREQ_ENDPOINT_CONFIG_KEY: DUMMY_HTTPREQ_ENDPOINT}
            assert (secret_name, "lego") in juju.secrets_granted

        def test_removes_pre_existing_secret_before_recreating(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN a secret already exists under the deterministic name (e.g. a previous
            # run created it but failed before setting plugin-config-secret-id)
            secret_name = extension.lego_plugin_config_secret_name("lego")
            juju.secrets[secret_name] = {HTTPREQ_ENDPOINT_CONFIG_KEY: "http://stale-endpoint"}

            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN the stale secret is removed before a fresh one is created
            assert secret_name in juju.secrets_removed
            assert juju.secrets[secret_name] == {HTTPREQ_ENDPOINT_CONFIG_KEY: DUMMY_HTTPREQ_ENDPOINT}

        def test_does_not_fail_when_no_pre_existing_secret(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN no secret exists yet under the deterministic name
            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN the missing-secret removal failure is swallowed and configuration proceeds
            secret_name = extension.lego_plugin_config_secret_name("lego")
            assert juju.secrets[secret_name] == {HTTPREQ_ENDPOINT_CONFIG_KEY: DUMMY_HTTPREQ_ENDPOINT}

        def test_skips_when_plugin_and_secret_id_already_set(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN the application already has both plugin and plugin-config-secret-id set
            juju.configured_applications.append(
                (TEST_MODEL.uri, "lego", {"plugin": "httpreq", "plugin-config-secret-id": "secret:existing"})
            )

            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN no further configuration happens
            assert len(juju.configured_applications) == 1
            assert juju.secrets == {}

        def test_does_not_skip_when_only_secret_id_set(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN the application has plugin-config-secret-id set but plugin is empty
            # (e.g. a partially-configured deployment)
            juju.configured_applications.append(
                (TEST_MODEL.uri, "lego", {"plugin-config-secret-id": "secret:existing"})
            )

            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN configuration still proceeds, since the app would otherwise stay blocked
            assert len(juju.configured_applications) == 2

        def test_does_not_skip_when_only_plugin_set(self, extension: LegoExtension, juju: JujuStub) -> None:
            # GIVEN the application has plugin set but plugin-config-secret-id is empty
            juju.configured_applications.append((TEST_MODEL.uri, "lego", {"plugin": "httpreq"}))

            # WHEN configure_lego is called
            extension.configure_lego(TEST_MODEL, "lego")

            # THEN configuration still proceeds, since the app would otherwise stay blocked
            assert len(juju.configured_applications) == 2
