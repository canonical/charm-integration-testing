# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CalledProcessError
from typing import Generator  # nosec

import pytest
from extensions.s3_integrator_minio_backend.extension import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_PATH,
    MINIO_SECRET_KEY,
    S3IntegratorMinIOBackendExtension,
)

from ..shared import JujuStub as JujuStubBase


@dataclass
class JujuStub(JujuStubBase):
    applications: dict[str, str] = field(default_factory=lambda: {"s3-app": "s3-integrator"})
    unit_ips: dict[str, str] = field(default_factory=lambda: {"s3-app-minio/leader": "10.0.0.1"})


class TestS3IntegratorMinIOBackendExtension:
    @pytest.fixture
    def juju(self) -> JujuStub:
        return JujuStub()

    @pytest.fixture
    def extension(self, juju: JujuStub) -> S3IntegratorMinIOBackendExtension:
        return S3IntegratorMinIOBackendExtension(juju, logging.getLogger("test"))

    class TestPostDeploy:
        def test_deploys_minio_if_s3_integrator_present(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with an s3-integrator application
            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN minio is deployed
            assert ("test-model", "minio", "s3-app-minio") in juju.deployed

        def test_ignores_non_s3_integrator_apps(self, juju: JujuStub) -> None:
            # GIVEN a model with no s3-integrator applications
            juju.applications = {"non-s3": "not-s3"}
            extension = S3IntegratorMinIOBackendExtension(juju, logging.getLogger("test"))

            # WHEN post_deploy is called
            extension.post_deploy("test-model")

            # THEN no deployments happen
            assert juju.deployed == []

    class TestDeployMinIO:
        def test_skips_if_endpoint_already_configured(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN an s3-integrator application that already has an endpoint configured
            juju.configured_applications.append(("test-model", "s3-app", {"endpoint": "http://existing:9000"}))

            # WHEN deploy_minio_s3_backend is called
            extension.deploy_minio_s3_backend("test-model", "s3-app")

            # THEN nothing is deployed or additionally configured
            assert juju.deployed == []
            assert len(juju.configured_applications) == 1  # unchanged from pre-populated value

        def test_deploy_flow_sets_up_everything(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
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
            ) in juju.configured_applications

            assert ("test-model", "s3-app", "0:10:00") in juju.waited_scaled
            assert ("test-model", "s3-app-minio", "0:10:00") in juju.waited_scaled

            assert juju.scp_calls == [
                ("test-model", str(Path("mc").resolve()), "s3-app-minio/leader:/usr/local/bin/mc")
            ]

            assert any("/usr/local/bin/mc alias set local" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("/usr/local/bin/mc mb" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("touch empty && /usr/local/bin/mc cp empty" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("&& rm empty" in cmd for _, _, cmd in juju.ssh_calls)

            assert (
                "test-model",
                "s3-app",
                {
                    "path": MINIO_PATH,
                    "endpoint": "http://10.0.0.1:9000",
                    "bucket": MINIO_BUCKET,
                },
            ) in juju.configured_applications

            assert (
                "test-model",
                "s3-app/leader",
                "sync-s3-credentials",
                {
                    "access-key": MINIO_ACCESS_KEY,
                    "secret-key": MINIO_SECRET_KEY,
                },
            ) in juju.actions

        def test_alias_retries_on_failure(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            def generate_results() -> Generator[CalledProcessError | str, None, None]:
                yield CalledProcessError(1, "bad-command")
                yield "Success"

            results = generate_results()

            def ssh_errors_once(model: str, target: str, command: str) -> None:
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

        def test_alias_max_attempts_exceeded(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            def ssh_errors(model: str, target: str, command: str) -> None:
                raise CalledProcessError(1, "bad-command")

            monkeypatch.setattr(juju, "ssh", ssh_errors)

            # GIVEN a ready extension with the client downloaded
            extension.minio_client_file = Path("mc")

            # WHEN set_minio_alias fails every attempt, THEN it errors
            with pytest.raises(CalledProcessError):
                extension.set_minio_alias("test-model", "s3-app", max_attempts=3, retry_sleep_seconds=0)

        def test_create_minio_bucket_creates_path(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a ready extension with the client downloaded
            extension.minio_client_file = Path("mc")

            # WHEN create_minio_bucket is called
            extension.create_minio_bucket("test-model", "s3-app")

            # THEN bucket is created
            assert any("/usr/local/bin/mc mb local/minio-bucket-for-testing" in cmd for _, _, cmd in juju.ssh_calls)

            # AND path is created in a single command
            assert any(
                "touch empty && /usr/local/bin/mc cp empty local/minio-bucket-for-testing/some-s3-path/ && rm empty"
                in cmd
                for _, _, cmd in juju.ssh_calls
            )

        def test_authenticate_s3_integrator_includes_path(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a ready extension
            # WHEN authenticate_s3_integrator is called
            extension.authenticate_s3_integrator("test-model", "s3-app")

            # THEN s3-integrator is configured with path, endpoint, and bucket
            assert (
                "test-model",
                "s3-app",
                {
                    "path": MINIO_PATH,
                    "endpoint": "http://10.0.0.1:9000",
                    "bucket": MINIO_BUCKET,
                },
            ) in juju.configured_applications

            # AND credentials are synced
            assert (
                "test-model",
                "s3-app/leader",
                "sync-s3-credentials",
                {
                    "access-key": MINIO_ACCESS_KEY,
                    "secret-key": MINIO_SECRET_KEY,
                },
            ) in juju.actions

    class TestPreRemove:
        def test_removes_minio_for_s3_integrator(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with s3-app and its associated minio application
            juju.applications = {"s3-app": "s3-integrator", "s3-app-minio": "minio"}

            # WHEN pre_remove is called for the s3 integrator
            extension.pre_remove("test-model", "s3-app")

            # THEN the minio app is removed
            assert ("test-model", "s3-app-minio") in juju.removed

        def test_waits_for_minio_removal(self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub) -> None:
            # GIVEN a model with s3-app and its associated minio application
            juju.applications = {"s3-app": "s3-integrator", "s3-app-minio": "minio"}

            # WHEN pre_remove is called
            extension.pre_remove("test-model", "s3-app")

            # THEN it waits for the minio app to be removed
            assert any("s3-app-minio" in apps for _, apps, _ in juju.waited_removal)

        def test_skips_application_not_in_model(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model where the application to be removed doesn't exist
            juju.applications = {}

            # WHEN pre_remove is called
            extension.pre_remove("test-model", "s3-app")

            # THEN nothing is removed
            assert juju.removed == []

        def test_skips_non_s3_integrator_apps(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with a non-s3-integrator application
            juju.applications = {"other-app": "other-charm"}

            # WHEN pre_remove is called for that application
            extension.pre_remove("test-model", "other-app")

            # THEN nothing is removed
            assert juju.removed == []

        def test_skips_if_minio_not_in_model(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with s3-app but no associated minio application
            juju.applications = {"s3-app": "s3-integrator"}

            # WHEN pre_remove is called
            extension.pre_remove("test-model", "s3-app")

            # THEN nothing is removed
            assert juju.removed == []

        def test_does_not_wait_if_nothing_removed(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a model with s3-app but no associated minio application
            juju.applications = {"s3-app": "s3-integrator"}

            # WHEN pre_remove is called
            extension.pre_remove("test-model", "s3-app")

            # THEN no wait for removal is triggered
            assert juju.waited_removal == []

    class TestGetMinioClientFile:
        def test_downloads_only_once(self, extension: S3IntegratorMinIOBackendExtension) -> None:
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
        def test_minio_application_name(self, extension: S3IntegratorMinIOBackendExtension) -> None:
            # GIVEN an s3 integrator name
            # WHEN minio_application is called
            result = extension.minio_application("s3-app")

            # THEN the correct minio app name is returned
            assert result == "s3-app-minio"

        def test_minio_unit_name(self, extension: S3IntegratorMinIOBackendExtension) -> None:
            # GIVEN an s3 integrator name
            # WHEN minio_unit is called
            result = extension.minio_unit("s3-app")

            # THEN it returns the leader unit
            assert result == "s3-app-minio/leader"

        def test_minio_address_builds_from_unit_ip(self, extension: S3IntegratorMinIOBackendExtension) -> None:
            # GIVEN a known unit IP
            # WHEN minio_address is called
            result = extension.minio_address("test-model", "s3-app")

            # THEN the URL is constructed correctly
            assert result == "http://10.0.0.1:9000"
