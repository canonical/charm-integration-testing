# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import urllib.request
from abc import ABC
from datetime import timedelta
from pathlib import Path

from juju import JujuBackend, JujuExtension

MINIO_CHARM = "minio"
S3_INTEGRATOR_CHARM = "s3-integrator"
MINIO_APPLICATION_NAME = "{s3_integrator_application}-minio"
MINIO_ACCESS_KEY = "minio-access-key-for-testing"  # nosec B105
MINIO_SECRET_KEY = "minio-secret-key-for-testing"  # nosec B105
MINIO_BUCKET = "minio-bucket-for-testing"
MINIO_ADDRESS = "http://{unit_ip}:9000"
MINIO_CLIENT_DOWNLOAD = "https://dl.min.io/client/mc/release/linux-amd64/mc"
MINIO_CLIENT_PATH = "/usr/local/bin/mc"
MINIO_CLIENT_MAKE_EXECUTABLE = "chmod +x {client_path}"
MINIO_CLIENT_AUTHENTICATE = "{client_path} alias set local {address} {access_key} {secret_key}"
MINIO_CLIENT_MAKE_BUCKET = "{client_path} mb local/{bucket}"


class S3IntegratorMinIOBackendExtension(JujuExtension, ABC):
    juju: JujuBackend
    logger: logging.Logger
    minio_client_file: Path | None = None

    def __init__(self, juju: JujuBackend, logger: logging.Logger, minio_client_file: Path | None = None):
        self.juju = juju
        self.logger = logger
        self.minio_client_file = minio_client_file

    def post_deploy(self, model: str) -> None:
        # Look for s3 integrator charms
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == S3_INTEGRATOR_CHARM:
                self.deploy_minio_s3_backend(model, application)

    def deploy_minio_s3_backend(self, model: str, s3_integrator_application: str) -> None:
        # Follows guide: https://discourse.charmhub.io/t/cos-lite-docs-set-up-minio-for-s3-testing/15211

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

        # Setup MinIO client
        self.setup_minio_client(model, s3_integrator_application)

        # Create MinIO bucket
        self.create_minio_bucket(model, s3_integrator_application)

        # Authenticate s3 integrator with the bucket
        self.authenticate_s3_integrator(model, s3_integrator_application)

    def minio_application(self, s3_integrator_application: str) -> str:
        return MINIO_APPLICATION_NAME.format(s3_integrator_application=s3_integrator_application)

    def minio_unit(self, s3_integrator_application: str) -> str:
        return f"{self.minio_application(s3_integrator_application)}/leader"

    def minio_address(self, model: str, s3_integrator_application: str) -> str:
        return MINIO_ADDRESS.format(unit_ip=self.juju.unit_ip(model, self.minio_unit(s3_integrator_application)))

    def setup_minio_client(self, model: str, s3_integrator_application: str) -> None:
        # Get the MinIO client file path
        minio_client_file = self.get_minio_client_file()

        # Copy client to the pod
        self.logger.info(f"Copying the MinIO client to '{self.minio_application(s3_integrator_application)}'")
        self.juju.scp(
            model, str(minio_client_file.resolve()), f"{self.minio_unit(s3_integrator_application)}:{MINIO_CLIENT_PATH}"
        )

        # Make client executable
        self.logger.info(f"Mark MinIO client in '{self.minio_application(s3_integrator_application)}' as executable")
        self.juju.ssh(
            model,
            self.minio_unit(s3_integrator_application),
            MINIO_CLIENT_MAKE_EXECUTABLE.format(client_path=MINIO_CLIENT_PATH),
        )

        # Authenticate client with MinIO
        self.logger.info(f"Authenticating MinIO client in '{self.minio_application(s3_integrator_application)}'")
        self.juju.ssh(
            model,
            self.minio_unit(s3_integrator_application),
            MINIO_CLIENT_AUTHENTICATE.format(
                client_path=MINIO_CLIENT_PATH,
                address=self.minio_address(model, s3_integrator_application),
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
            ),
        )

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

    def authenticate_s3_integrator(self, model: str, s3_integrator_application: str) -> None:
        self.logger.info(
            f"Configuring s3 integrator '{s3_integrator_application}' to use '{self.minio_application(s3_integrator_application)}'"
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

        # Point s3 integrator at MinIO bucket
        self.juju.configure_application(
            model,
            s3_integrator_application,
            {
                "endpoint": self.minio_address(model, s3_integrator_application),
                "bucket": MINIO_BUCKET,
            },
        )
