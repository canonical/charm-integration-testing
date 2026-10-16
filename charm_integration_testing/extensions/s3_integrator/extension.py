# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time
from datetime import timedelta
from typing import MutableMapping

from juju import JujuBackend, JujuExtension, JujuModelHandle
from resource_registry import ResourceRegistry
from s3_storage import S3Provider
from s3_storage.storage import S3_PATH

_INTEGRATOR_WAIT_TIMEOUT = timedelta(minutes=10)
_UNIT_POLL_INTERVAL = 1


class S3IntegratorExtension(JujuExtension):
    def __init__(
        self,
        juju: JujuBackend,
        providers: MutableMapping[str, S3Provider],
        resource_registry: ResourceRegistry,
        cleanup_buckets: bool,
    ) -> None:
        self.juju = juju
        self.providers = providers
        self.resource_registry = resource_registry
        self.cleanup_buckets = cleanup_buckets

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        source_handle = JujuModelHandle(controller=source, model=model)
        target_handle = JujuModelHandle(controller=target, model=model)
        provider = self.providers.pop(source_handle.resource_id, None)
        if provider is not None:
            self.providers.setdefault(target_handle.resource_id, provider)

    def post_deploy(self, model: JujuModelHandle) -> None:
        for application, info in self.juju.list_applications(model).items():
            if info.charm != "s3-integrator":
                continue
            if self.juju.get_application_config(model, application).get("endpoint"):
                continue
            provider = self.providers.get(model.resource_id)
            if provider is None:
                raise RuntimeError(
                    f"S3 integrator '{application}' in model '{model.resource_id}' has no S3 provider; "
                    "configure this model with either s3-swift-secret variables (S3_OS_* and "
                    "S3_CLEANUP_ACCESS_KEY/S3_CLEANUP_SECRET_KEY) or S3_ENDPOINT, S3_ACCESS_KEY, and "
                    "S3_SECRET_KEY"
                )
            self.juju.wait_application_scaled(model, application, _INTEGRATOR_WAIT_TIMEOUT)
            self.juju.wait_application_settled(model, application, _INTEGRATOR_WAIT_TIMEOUT)
            if self.juju.num_units(model, application) == 0:
                continue
            bucket_resource = provider.new_bucket()
            self.resource_registry.register(
                handle=bucket_resource.handle,
                destroyer=provider.bucket_destroyer(bucket_resource) if self.cleanup_buckets else None,
                parent=provider.handle,
            )
            bucket_name, credentials = bucket_resource.provision()
            # Set the endpoint last: a failed action must not look fully configured on redeploy.
            task = self.juju.run_action(
                model,
                f"{application}/leader",
                "sync-s3-credentials",
                {"access-key": credentials.access_key, "secret-key": credentials.secret_key},
            )
            if task.return_code != 0 or task.status != "completed":
                raise RuntimeError(f"S3 credential synchronization failed for {application} (status {task.status})")
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

    def post_scale(self, model: JujuModelHandle) -> None:
        self.post_deploy(model)

    def post_scale_application(self, model: JujuModelHandle, application: str, num: int) -> None:
        app_info = self.juju.list_applications(model).get(application)
        if (
            num > 0
            and app_info is not None
            and app_info.charm == "s3-integrator"
            and not self.juju.get_application_config(model, application).get("endpoint")
            and model.resource_id in self.providers
        ):
            self._wait_for_first_unit(model, application)
        self.post_deploy(model)

    def _wait_for_first_unit(self, model: JujuModelHandle, application: str) -> None:
        deadline = time.monotonic() + _INTEGRATOR_WAIT_TIMEOUT.total_seconds()
        while self.juju.num_units(model, application) == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for an s3-integrator unit in '{model.resource_id}'")
            time.sleep(_UNIT_POLL_INTERVAL)
