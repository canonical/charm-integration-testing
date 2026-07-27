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
    MINIO_CLIENT_INSTALL_K8S,
    MINIO_CLIENT_PATH,
    MINIO_CLIENT_STAGING_PATH,
    MINIO_PATH,
    MINIO_SECRET_KEY,
    MINIO_SERVER_DATA_DIR,
    MINIO_SERVER_PATH,
    MINIO_SERVER_STAGING_PATH,
    UBUNTU_CHARM,
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
            # GIVEN a k8s model and a ready extension with the client downloaded
            juju.is_k8s = True
            extension.minio_client_file = Path("mc")

            # WHEN deploy_minio_s3_backend is called
            extension.deploy_minio_s3_backend("test-model", "s3-app")

            # THEN the minio charm is deployed and configured
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
                ("test-model", str(Path("mc").resolve()), f"s3-app-minio/leader:{MINIO_CLIENT_STAGING_PATH}")
            ]

            assert any(
                MINIO_CLIENT_INSTALL_K8S.format(staging_path=MINIO_CLIENT_STAGING_PATH, client_path=MINIO_CLIENT_PATH)
                in cmd
                for _, _, cmd in juju.ssh_calls
            )

            assert any("/usr/local/bin/mc alias set local" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("/usr/local/bin/mc mb" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("touch empty && /usr/local/bin/mc cp empty" in cmd for _, _, cmd in juju.ssh_calls)
            assert any("&& rm empty" in cmd for _, _, cmd in juju.ssh_calls)

            assert (
                "test-model",
                "s3-app",
                {
                    "path": MINIO_PATH,
                    "endpoint": "http://s3-app-minio.test-model.svc:9000",
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

        def test_deploy_flow_machine_uses_binary(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a machine (non-k8s) model and a ready extension with binaries pre-downloaded
            juju.is_k8s = False
            extension.minio_client_file = Path("mc")
            extension.minio_server_file = Path("minio")

            # WHEN deploy_minio_s3_backend is called
            extension.deploy_minio_s3_backend("test-model", "s3-app")

            # THEN ubuntu is deployed (not the minio charm)
            assert ("test-model", UBUNTU_CHARM, "s3-app-minio") in juju.deployed
            assert ("test-model", "minio", "s3-app-minio") not in juju.deployed

            # AND minio server binary is uploaded, installed, and started via systemd-run
            assert (
                "test-model",
                str(Path("minio").resolve()),
                f"s3-app-minio/leader:{MINIO_SERVER_STAGING_PATH}",
            ) in juju.scp_calls
            assert any(
                f"sudo install -m 755 {MINIO_SERVER_STAGING_PATH} {MINIO_SERVER_PATH}" in cmd
                for _, _, cmd in juju.ssh_calls
            )
            assert any(f"mkdir -p {MINIO_SERVER_DATA_DIR}" in cmd for _, _, cmd in juju.ssh_calls)
            assert any(
                "sudo systemctl show minio-server.service --property=LoadState" in cmd and MINIO_SERVER_DATA_DIR in cmd
                for _, _, cmd in juju.ssh_calls
            )

            # AND the minio charm access-key/secret-key config is NOT used
            assert not any(
                app == "s3-app-minio" and "access-key" in cfg for _, app, cfg in juju.configured_applications
            )

            # AND mc client, bucket, and s3-integrator auth are set up as normal
            assert (
                "test-model",
                str(Path("mc").resolve()),
                f"s3-app-minio/leader:{MINIO_CLIENT_STAGING_PATH}",
            ) in juju.scp_calls
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
                    "endpoint": "http://s3-app-minio.test-model.svc:9000",
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

        def test_minio_address_uses_k8s_service_hostname_for_k8s_models(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a k8s model
            juju.is_k8s = True

            # WHEN minio_address is called
            result = extension.minio_address("test-model", "s3-app")

            # THEN the stable k8s Service DNS name is used instead of the pod IP, since the
            # pod (and its IP) can change after events such as model migration
            assert result == "http://s3-app-minio.test-model.svc:9000"

        def test_minio_address_uses_model_name_segment_of_a_model_uri(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a k8s model passed as a "controller:model-name" URI, as happens during
            # JujuClient.deploy_bundles()
            juju.is_k8s = True

            # WHEN minio_address is called
            result = extension.minio_address("test-controller:test-model", "s3-app")

            # THEN only the model-name segment is used as the k8s namespace
            assert result == "http://s3-app-minio.test-model.svc:9000"

        def test_minio_address_builds_from_unit_ip_for_machine_models(
            self, extension: S3IntegratorMinIOBackendExtension, juju: JujuStub
        ) -> None:
            # GIVEN a machine (non-k8s) model with a known unit IP
            juju.is_k8s = False

            # WHEN minio_address is called
            result = extension.minio_address("test-model", "s3-app")

            # THEN the URL is constructed from the unit IP
            assert result == "http://10.0.0.1:9000"
