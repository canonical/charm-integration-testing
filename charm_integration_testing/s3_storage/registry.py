# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable, Mapping
from pathlib import Path

from .configuration import provider_from_config
from .provider import S3Provider
from .storage import S3StorageHandle


class S3ProviderRegistry:
    """Resolve cloud-scoped configuration and reuse providers with the same identity."""

    def __init__(
        self,
        configs: Mapping[str, Path],
        provider_factory: Callable[[Path], S3Provider] = provider_from_config,
    ) -> None:
        self._configs = dict(configs)
        self._provider_factory = provider_factory
        self._providers_by_cloud: dict[str, S3Provider] = {}
        self._providers: dict[S3StorageHandle, S3Provider] = {}

    def provider_for_cloud(self, cloud: str) -> S3Provider:
        if cloud not in self._providers_by_cloud:
            try:
                config = self._configs[cloud]
            except KeyError as exc:
                raise ValueError(f"No S3 configuration for Juju cloud '{cloud}'; set S3_CONFIG_<cloud>") from exc
            provider = self._provider_factory(config)
            self._providers_by_cloud[cloud] = self._providers.setdefault(provider.handle, provider)
        return self._providers_by_cloud[cloud]

    def cleanup_pending_resources(self) -> None:
        """Attempt pending cleanup for every used provider before dependencies close."""
        failures: list[Exception] = []
        for provider in self._providers.values():
            try:
                provider.cleanup_pending_resources()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError(f"Pending S3 cleanup failed for {len(failures)} provider(s)") from failures[0]
