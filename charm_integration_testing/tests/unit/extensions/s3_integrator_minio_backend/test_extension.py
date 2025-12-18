# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CalledProcessError  # nosec

import pytest
from extensions.s3_integrator_minio_backend.extension import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_SECRET_KEY,
    S3IntegratorMinIOBackendExtension,
)


@dataclass
class JujuStub:
    deployed: list = field(default_factory=list)
    configured: list = field(default_factory=list)
    waited_scaled: list = field(default_factory=list)
    waited_settled: list = field(default_factory=list)
    scp_calls: list = field(default_factory=list)
    ssh_calls: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    applications: dict = field(default_factory=lambda: {"s3-app": "s3-integrator"})
    unit_ips: dict = field(default_factory=lambda: {"s3-app-minio/leader": "10.0.0.1"})

    def list_applications(self, model: str):
        return self.applications.keys()

    def application_charm(self, model: str, application: str):
        return self.applications[application]

    def deploy_application(self, model: str, charm: str, application: str):
        self.deployed.append((model, charm, application))

    def configure_application(self, model: str, application: str, values: dict):
        self.configured.append((model, application, values))

    def wait_application_scaled(self, model: str, application: str, timeout):
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout):
        self.waited_settled.append((model, application, str(timeout)))

    def scp(self, model: str, source: str, destination: str):
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: str, target: str, command: str):
        self.ssh_calls.append((model, target, command))

    def run_action(self, model: str, unit: str, action: str, params: dict):
        self.actions.append((model, unit, action, params))

    def unit_ip(self, model: str, unit: str):
        return self.unit_ips[unit]


class TestS3IntegratorMinIOBackendExtension:
    @pytest.fixture
    def juju(self):
        return JujuStub()

    @pytest.fixture
    def extension(self, juju):
        return S3IntegratorMinIOBackendExtension(juju, logging.getLogger("test"))

    class TestPostDeploy:
        def test_deploys_minio_if_s3_integrator_present(self, extension, juju):
            # GIVEN a model with an s3-integrator application
            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN minio is deployed
            assert ("test-model", "minio", "s3-app-minio") in juju.deployed

        def test_ignores_non_s3_integrator_apps(self, juju):
            # GIVEN a model with no s3-integrator applications
            juju.applications = {"non-s3": "not-s3"}
            extension = S3IntegratorMinIOBackendExtension(juju, logging.getLogger("test"))

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no deployments happen
            assert juju.deployed == []

    class TestDeployMinIO:
        def test_deploy_flow_sets_up_everything(self, extension, juju):
            # GIVEN a ready extension with the client downloaded
            extension.minio_client_file = Path("mc")

            # WHEN deploy_minio_s3_backend is called
            extension.deploy_minio_s3_backend("test-model", "s3-app")

            # THEN minio is deployed, configured, waited on, authenticated, and connected
            assert ("test-model", "minio", "s3-app-minio") in juju.deployed

            assert (
                "test-model",
                "s3-app-minio",
                {
                    "access-key": MINIO_ACCESS_KEY,
                    "secret-key": MINIO_SECRET_KEY,
                },
            ) in juju.configured

            assert ("test-model", "s3-app", "0:10:00") in juju.waited_scaled
            assert ("test-model", "s3-app-minio", "0:10:00") in juju.waited_scaled

            assert juju.scp_calls == [
                ("test-model", str(Path("mc").resolve()), "s3-app-minio/leader:/usr/local/bin/mc")
            ]

            assert any("/usr/local/bin/mc alias set local" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("/usr/local/bin/mc mb" in cmd for _, _, cmd in juju.ssh_calls)

            assert (
                "test-model",
                "s3-app/leader",
                "sync-s3-credentials",
                {
                    "access-key": MINIO_ACCESS_KEY,
                    "secret-key": MINIO_SECRET_KEY,
                },
            ) in juju.actions

            assert (
                "test-model",
                "s3-app",
                {
                    "endpoint": "http://10.0.0.1:9000",
                    "bucket": MINIO_BUCKET,
                },
            ) in juju.configured

        def test_alias_retries_on_failure(self, extension, juju, monkeypatch):
            def generate_results():
                yield CalledProcessError(1, "bad-command")
                yield "Success"

            results = generate_results()

            def ssh_errors_once(model: str, target: str, command: str):
                result = next(results)
                if isinstance(result, CalledProcessError):
                    raise result
                juju.ssh_calls.append((model, target, command))

            monkeypatch.setattr(juju, "ssh", ssh_errors_once)

            # GIVEN a ready extension with the client downloaded
            extension.minio_client_file = Path("mc")

            # WHEN set_minio_alias is called
            extension.set_minio_alias("test-model", "s3-app", max_attempts=3, retry_sleep_seconds=0)

            # THEN the alias command runs successfully
            assert any("/usr/local/bin/mc alias set local" in cmd for _, _, cmd in juju.ssh_calls)

        def test_alias_max_attempts_exceeded(self, extension, juju, monkeypatch):
            def ssh_errors(model: str, target: str, command: str):
                raise CalledProcessError(1, "bad-command")

            monkeypatch.setattr(juju, "ssh", ssh_errors)

            # GIVEN a ready extension with the client downloaded
            extension.minio_client_file = Path("mc")

            # WHEN set_minio_alias fails every attempt, THEN it errors
            with pytest.raises(CalledProcessError):
                extension.set_minio_alias("test-model", "s3-app", max_attempts=3, retry_sleep_seconds=0)

    class TestGetMinioClientFile:
        def test_downloads_only_once(self, extension):
            # GIVEN no client downloaded
            extension.minio_client_file = None

            # WHEN called
            path = extension.get_minio_client_file()

            # THEN it is cached
            assert path == extension.minio_client_file

            # WHEN called again
            path2 = extension.get_minio_client_file()

            # THEN it reuses the file
            assert path == path2

    class TestUtilityFunctions:
        def test_minio_application_name(self, extension):
            # GIVEN an s3 integrator name
            # WHEN minio_application is called
            result = extension.minio_application("s3-app")

            # THEN the correct minio app name is returned
            assert result == "s3-app-minio"

        def test_minio_unit_name(self, extension):
            # GIVEN an s3 integrator name
            # WHEN minio_unit is called
            result = extension.minio_unit("s3-app")

            # THEN it returns the leader unit
            assert result == "s3-app-minio/leader"

        def test_minio_address_builds_from_unit_ip(self, extension):
            # GIVEN a known unit IP
            # WHEN minio_address is called
            result = extension.minio_address("test-model", "s3-app")

            # THEN the URL is constructed correctly
            assert result == "http://10.0.0.1:9000"
