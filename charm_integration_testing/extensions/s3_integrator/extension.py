# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from typing import MutableMapping

from juju import JujuBackend, JujuExtension, JujuModelHandle
from resource_registry import ResourceRegistry
from s3_storage import S3Provider
from s3_storage.storage import S3_PATH


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
                    "configure this model's environment with s3-swift-secret or S3_ENDPOINT credentials"
                )
            self.juju.wait_application_scaled(model, application, timedelta(minutes=10))
            self.juju.wait_application_settled(model, application, timedelta(minutes=10))
            if self.juju.num_units(model, application) == 0:
                continue
            bucket_resource = provider.new_bucket()
            self.resource_registry.register(
                handle=bucket_resource.handle,
                destroyer=provider.bucket_destroyer(bucket_resource) if self.cleanup_buckets else None,
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
