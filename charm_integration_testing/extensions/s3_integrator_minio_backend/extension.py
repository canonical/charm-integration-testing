# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
import urllib.request
from abc import ABC
from datetime import timedelta
from pathlib import Path
from subprocess import CalledProcessError  # nosec

from juju import JujuBackend, JujuExtension

MINIO_CHARM = "minio"
UBUNTU_CHARM = "ubuntu"
S3_INTEGRATOR_CHARM = "s3-integrator"
MINIO_APPLICATION_NAME = "{s3_integrator_application}-minio"
MINIO_ACCESS_KEY = "minio-access-key-for-testing"  # nosec B105
MINIO_SECRET_KEY = "minio-secret-key-for-testing"  # nosec B105
MINIO_PATH = "some-s3-path"
MINIO_BUCKET = "minio-bucket-for-testing"
MINIO_ADDRESS = "http://{unit_ip}:9000"
# Juju creates a k8s Service named after the application in the model's namespace
# (which shares the model's name), so this hostname is stable across pod restarts
# and model migrations, unlike the pod IP. The suffix is deliberately just ".svc"
# (not ".svc.cluster.local") so resolution relies on the pod's search domains
# instead of assuming every cluster uses the default "cluster.local" domain.
MINIO_K8S_SERVICE_ADDRESS = "http://{service}.{namespace}.svc:9000"
MINIO_CLIENT_DOWNLOAD = "https://dl.min.io/client/mc/release/linux-amd64/mc"
MINIO_CLIENT_PATH = "/usr/local/bin/mc"
MINIO_CLIENT_STAGING_PATH = "/tmp/mc"  # nosec B108
MINIO_CLIENT_INSTALL = "sudo install -m 755 {staging_path} {client_path}"
MINIO_CLIENT_INSTALL_K8S = "install -m 755 {staging_path} {client_path}"
MINIO_CLIENT_SET_ALIAS = "{client_path} alias set local {address} {access_key} {secret_key}"
MINIO_CLIENT_MAKE_BUCKET = "{client_path} mb local/{bucket}"
MINIO_CLIENT_MAKE_PATH = "touch empty && {client_path} cp empty local/{bucket}/{path}/ && rm empty"
MINIO_SERVER_VERSION = "RELEASE.2025-10-15T17-29-55Z"
MINIO_SERVER_DOWNLOAD = f"https://github.com/minio/minio/releases/download/{MINIO_SERVER_VERSION}/minio"
MINIO_SERVER_STAGING_PATH = "/tmp/minio-server"  # nosec B108
MINIO_SERVER_PATH = "/usr/local/bin/minio"
MINIO_SERVER_INSTALL = "sudo install -m 755 {staging_path} {server_path}"
MINIO_SERVER_DATA_DIR = "/home/ubuntu/minio-data"
MINIO_SERVER_START = (
    "if sudo systemctl show minio-server.service --property=LoadState 2>/dev/null"
    " | grep -q LoadState=loaded;"
    " then sudo systemctl restart minio-server.service;"
    " else sudo systemd-run --unit=minio-server"
    " -E MINIO_ROOT_USER={access_key}"
    " -E MINIO_ROOT_PASSWORD={secret_key}"
    " {server_path} server {data_dir}; fi"
)


class S3IntegratorMinIOBackendExtension(JujuExtension, ABC):
    juju: JujuBackend
    logger: logging.Logger
    minio_client_file: Path | None = None
    minio_server_file: Path | None = None

    def __init__(
        self,
        juju: JujuBackend,
        logger: logging.Logger,
        minio_client_file: Path | None = None,
        minio_server_file: Path | None = None,
    ) -> None:
        self.juju = juju
        self.logger = logger
        self.minio_client_file = minio_client_file
        self.minio_server_file = minio_server_file

    def post_deploy(self, model: str) -> None:
        # Look for s3 integrator charms
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == S3_INTEGRATOR_CHARM:
                self.deploy_minio_s3_backend(model, application)

    def pre_remove(self, model: str, *applications: str) -> None:
        # Remove MinIO applications related to s3 integrator applications being removed
        all_applications = self.juju.list_applications(model)
        to_remove: list[str] = []
        for application in applications:
            if application not in all_applications:
                continue
            if all_applications[application].charm != S3_INTEGRATOR_CHARM:
                continue
            minio_application = self.minio_application(application)
            if minio_application not in all_applications:
                continue
            to_remove.append(minio_application)

        # If no applications to remove do nothing
        if not to_remove:
            return

        # Remove applications
        self.logger.info(f"Removing MinIO applications '{to_remove}' related to s3 integrators '{applications}'")
        self.juju.remove_applications(model, *to_remove)

        # Wait for applications to be removed
        self.logger.info(f"Waiting for MinIO applications related to removed s3 integrators to be removed: {to_remove}")
        self.juju.wait_for_removal(model, to_remove, timeout=timedelta(minutes=15))

    def deploy_minio_s3_backend(self, model: str, s3_integrator_application: str) -> None:
        # Follows guide: https://discourse.charmhub.io/t/cos-lite-docs-set-up-minio-for-s3-testing/15211

        # Skip if already configured
        if self.juju.get_application_config(model, s3_integrator_application).get("endpoint"):
            self.logger.info(f"Application '{s3_integrator_application}' already has endpoint set, skipping")
            return

        # Deploy minio backend appropriate for the model platform
        if self.juju.is_k8s_model(model):
            self._deploy_minio_charm(model, s3_integrator_application)
        else:
            self._deploy_minio_binary(model, s3_integrator_application)

        # Setup MinIO client
        self.setup_minio_client(model, s3_integrator_application)

        # Create MinIO bucket
        self.create_minio_bucket(model, s3_integrator_application)

        # Authenticate s3 integrator with the bucket
        self.authenticate_s3_integrator(model, s3_integrator_application)

    def _deploy_minio_charm(self, model: str, s3_integrator_application: str) -> None:
        """Deploy MinIO using the minio k8s charm."""
        # Deploy MinIO
        self.logger.info(
            f"Deploying MinIO application '{self.minio_application(s3_integrator_application)}' for s3 integrator '{s3_integrator_application}'"
        )
        self.juju.deploy_application(model, MINIO_CHARM, application=self.minio_application(s3_integrator_application))

        # Set access key and secret key configuration
        self.logger.info(f"Setting access key and secret key for '{self.minio_application(s3_integrator_application)}'")
        self.juju.configure_application(
            model,
            self.minio_application(s3_integrator_application),
            {
                "access-key": MINIO_ACCESS_KEY,
                "secret-key": MINIO_SECRET_KEY,
            },
        )

        # Wait for applications to be scaled
        for application in (s3_integrator_application, self.minio_application(s3_integrator_application)):
            self.logger.info(f"Waiting for application '{application}' to be scaled")
            self.juju.wait_application_scaled(model, application, timedelta(minutes=10))

        # Wait for units to settle
        for application in (s3_integrator_application, self.minio_application(s3_integrator_application)):
            self.logger.info(f"Waiting for application '{application}' units to be settled")
            self.juju.wait_application_settled(model, application, timedelta(minutes=10))

    def _deploy_minio_binary(self, model: str, s3_integrator_application: str) -> None:
        """Deploy MinIO by uploading its binary to an ubuntu machine charm, for non-k8s models."""
        # Deploy ubuntu charm as the minio host
        self.logger.info(
            f"Deploying ubuntu application '{self.minio_application(s3_integrator_application)}' as minio host for s3 integrator '{s3_integrator_application}'"
        )
        self.juju.deploy_application(model, UBUNTU_CHARM, application=self.minio_application(s3_integrator_application))

        # Wait for applications to be scaled
        for application in (s3_integrator_application, self.minio_application(s3_integrator_application)):
            self.logger.info(f"Waiting for application '{application}' to be scaled")
            self.juju.wait_application_scaled(model, application, timedelta(minutes=10))

        # Wait for units to settle
        for application in (s3_integrator_application, self.minio_application(s3_integrator_application)):
            self.logger.info(f"Waiting for application '{application}' units to be settled")
            self.juju.wait_application_settled(model, application, timedelta(minutes=10))

        # Install minio server binary
        unit = self.minio_unit(s3_integrator_application)
        minio_server_file = self.get_minio_server_file()
        self.logger.info(f"Copying minio server binary to '{self.minio_application(s3_integrator_application)}'")
        self.juju.scp(model, str(minio_server_file.resolve()), f"{unit}:{MINIO_SERVER_STAGING_PATH}")
        self.juju.ssh(
            model,
            unit,
            MINIO_SERVER_INSTALL.format(staging_path=MINIO_SERVER_STAGING_PATH, server_path=MINIO_SERVER_PATH),
        )

        # Create data directory and start the server via systemd-run
        self.juju.ssh(model, unit, f"mkdir -p {MINIO_SERVER_DATA_DIR}")
        self.logger.info(f"Starting minio server on '{self.minio_application(s3_integrator_application)}'")
        self.juju.ssh(
            model,
            unit,
            MINIO_SERVER_START.format(
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
                server_path=MINIO_SERVER_PATH,
                data_dir=MINIO_SERVER_DATA_DIR,
            ),
        )

    def minio_application(self, s3_integrator_application: str) -> str:
        return MINIO_APPLICATION_NAME.format(s3_integrator_application=s3_integrator_application)

    def minio_unit(self, s3_integrator_application: str) -> str:
        return f"{self.minio_application(s3_integrator_application)}/leader"

    def _model_namespace(self, model: str) -> str:
        # `model` may be a bare model name or a "controller:model-name" URI (as passed by
        # JujuClient.deploy_bundles). The k8s namespace is always just the model-name segment.
        return model.rpartition(":")[-1]

    def minio_address(self, model: str, s3_integrator_application: str) -> str:
        # For k8s models, use the stable Kubernetes Service DNS name rather than the pod IP,
        # since the pod (and its IP) can be recreated after events such as model migration.
        if self.juju.is_k8s_model(model):
            return MINIO_K8S_SERVICE_ADDRESS.format(
                service=self.minio_application(s3_integrator_application),
                namespace=self._model_namespace(model),
            )
        return MINIO_ADDRESS.format(unit_ip=self.juju.unit_ip(model, self.minio_unit(s3_integrator_application)))

    def setup_minio_client(self, model: str, s3_integrator_application: str) -> None:
        # Get the MinIO client file path
        minio_client_file = self.get_minio_client_file()

        # Copy client to a staging path writable by the ubuntu user
        self.logger.info(f"Copying the MinIO client to '{self.minio_application(s3_integrator_application)}'")
        self.juju.scp(
            model,
            str(minio_client_file.resolve()),
            f"{self.minio_unit(s3_integrator_application)}:{MINIO_CLIENT_STAGING_PATH}",
        )

        # Install to final path with correct permissions
        # k8s containers run as root (no sudo); machine units need sudo for /usr/local/bin
        self.logger.info(f"Installing MinIO client in '{self.minio_application(s3_integrator_application)}'")
        install_cmd = MINIO_CLIENT_INSTALL_K8S if self.juju.is_k8s_model(model) else MINIO_CLIENT_INSTALL
        self.juju.ssh(
            model,
            self.minio_unit(s3_integrator_application),
            install_cmd.format(staging_path=MINIO_CLIENT_STAGING_PATH, client_path=MINIO_CLIENT_PATH),
        )

        # Set MinIO alias
        self.set_minio_alias(model, s3_integrator_application, max_attempts=3, retry_sleep_seconds=10)

    def get_minio_client_file(self) -> Path:
        # Only download if not downloaded
        if self.minio_client_file is None:
            self.logger.info("Downloading MinIO client")
            # As a snap Juju cannot access /tmp, so just download into the current folder
            # Also security warning "Allowing use of file:/ or custom schemes is often unexpected."
            # does not apply to hardcoded URL
            file_path, _ = urllib.request.urlretrieve(MINIO_CLIENT_DOWNLOAD, "mc")  # nosec B310
            self.minio_client_file = Path(file_path)

        # Return file
        return self.minio_client_file

    def get_minio_server_file(self) -> Path:
        # Only download if not downloaded
        if self.minio_server_file is None:
            self.logger.info("Downloading MinIO server")
            file_path, _ = urllib.request.urlretrieve(MINIO_SERVER_DOWNLOAD, "minio")  # nosec B310
            self.minio_server_file = Path(file_path)

        # Return file
        return self.minio_server_file

    def create_minio_bucket(self, model: str, s3_integrator_application: str) -> None:
        self.logger.info(
            f"Creating the MinIO bucket '{MINIO_BUCKET}' in '{self.minio_application(s3_integrator_application)}'"
        )
        self.juju.ssh(
            model,
            self.minio_unit(s3_integrator_application),
            MINIO_CLIENT_MAKE_BUCKET.format(
                client_path=MINIO_CLIENT_PATH,
                bucket=MINIO_BUCKET,
            ),
        )

        # This is a workaround to ensure the path exists for spark-history-server-k8s
        # See:
        # - issue: Path must be provided
        #   link: https://github.com/canonical/spark-history-server-k8s-operator/issues/126
        # - issue: Something must exist at the path
        #   link: https://github.com/canonical/spark-history-server-k8s-operator/issues/127
        self.logger.info(
            f"Creating the MinIO path '{MINIO_BUCKET}/{MINIO_PATH}' in '{self.minio_application(s3_integrator_application)}'"
        )
        self.juju.ssh(
            model,
            self.minio_unit(s3_integrator_application),
            MINIO_CLIENT_MAKE_PATH.format(
                client_path=MINIO_CLIENT_PATH,
                bucket=MINIO_BUCKET,
                path=MINIO_PATH,
            ),
        )

    def authenticate_s3_integrator(self, model: str, s3_integrator_application: str) -> None:
        self.logger.info(
            f"Configuring s3 integrator '{s3_integrator_application}' to use '{self.minio_application(s3_integrator_application)}'"
        )

        # Point s3 integrator at MinIO bucket
        self.juju.configure_application(
            model,
            s3_integrator_application,
            {
                "path": MINIO_PATH,
                "endpoint": self.minio_address(model, s3_integrator_application),
                "bucket": MINIO_BUCKET,
            },
        )

        # Sync MinIO credentials to s3 integrator
        self.juju.run_action(
            model,
            f"{s3_integrator_application}/leader",
            "sync-s3-credentials",
            {
                "access-key": MINIO_ACCESS_KEY,
                "secret-key": MINIO_SECRET_KEY,
            },
        )

    def set_minio_alias(
        self, model: str, s3_integrator_application: str, max_attempts: int = 3, retry_sleep_seconds: int = 10
    ) -> None:
        self.logger.info(f"Setting MinIO alias in '{self.minio_application(s3_integrator_application)}'")
        for attempt in range(max_attempts):
            self.logger.info(f"Alias attempt {attempt + 1} of {max_attempts}")
            try:
                self.juju.ssh(
                    model,
                    self.minio_unit(s3_integrator_application),
                    MINIO_CLIENT_SET_ALIAS.format(
                        client_path=MINIO_CLIENT_PATH,
                        address=self.minio_address(model, s3_integrator_application),
                        access_key=MINIO_ACCESS_KEY,
                        secret_key=MINIO_SECRET_KEY,
                    ),
                )
                return
            except CalledProcessError as error:
                self.logger.warning(f"Alias attempt {attempt + 1} of {max_attempts} failed with error: {error}.")
                if attempt + 1 == max_attempts:
                    raise
                time.sleep(retry_sleep_seconds)
