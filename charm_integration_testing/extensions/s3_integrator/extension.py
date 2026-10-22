# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time
from collections.abc import Callable
from datetime import timedelta

from juju import JujuBackend, JujuExtension, JujuModelHandle
from s3_storage import S3Provider, S3ProviderRegistry, S3ResourceRegistry
from s3_storage.storage import S3_PATH

_INTEGRATOR_WAIT_TIMEOUT = timedelta(minutes=10)
_UNIT_POLL_INTERVAL = 1


class S3IntegratorExtension(JujuExtension):
    def __init__(
        self,
        juju: JujuBackend,
        providers: S3ProviderRegistry,
        resources_for_provider: Callable[[S3Provider], S3ResourceRegistry],
    ) -> None:
        self.juju = juju
        self.providers = providers
        self.resources_for_provider = resources_for_provider

    def post_deploy(self, model: JujuModelHandle) -> None:
        for application, info in self.juju.list_applications(model).items():
            self._configure_integrator(model, application, info.charm)

    def post_scale_application(self, model: JujuModelHandle, application: str, num: int) -> None:
        # A zero-unit deployment has no leader to configure; finish setup when it is scaled up.
        if num <= 0:
            return
        app_info = self.juju.list_applications(model).get(application)
        if app_info is not None:
            self._configure_integrator(model, application, app_info.charm, wait_for_scale=False)

    def _configure_integrator(
        self, model: JujuModelHandle, application: str, charm: str, *, wait_for_scale: bool = True
    ) -> None:
        if charm != "s3-integrator":
            return
        if self.juju.get_application_config(model, application).get("endpoint"):
            return
        if wait_for_scale:
            self.juju.wait_application_scaled(model, application, _INTEGRATOR_WAIT_TIMEOUT)
        else:
            self._wait_for_first_unit(model, application)
        if self.juju.num_units(model, application) == 0:
            return
        provider = self.providers.provider_for_cloud(self.juju.cloud_for_model(model))
        self.juju.wait_application_settled(model, application, _INTEGRATOR_WAIT_TIMEOUT)
        bucket_resource = self.resources_for_provider(provider).new_bucket()
        bucket_name, credentials = bucket_resource.provision()
        # Set the endpoint last: a failed action must not look fully configured on redeploy.
        task = self.juju.run_action(
            model,
            f"{application}/leader",
            "sync-s3-credentials",
            {"access-key": credentials.access_key, "secret-key": credentials.secret_key},
        )
        if task.return_code != 0 or task.status != "completed":
            raise RuntimeError(
                f"S3 credential synchronization failed for {application} in model {model.uri} "
                f"(action {task.id}, status {task.status}, return code {task.return_code})"
            )
        self.juju.configure_application(
            model,
            application,
            {
                "endpoint": provider.endpoint,
                "bucket": bucket_name,
                "path": S3_PATH,
                "region": provider.region,
                "s3-uri-style": "path",
                "s3-api-version": "s3v4",
            },
        )

    def _wait_for_first_unit(self, model: JujuModelHandle, application: str) -> None:
        deadline = time.monotonic() + _INTEGRATOR_WAIT_TIMEOUT.total_seconds()
        while self.juju.num_units(model, application) == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for an s3-integrator unit in '{model.resource_id}'")
            time.sleep(_UNIT_POLL_INTERVAL)
