# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable, Mapping
from pathlib import Path

from .configuration import provider_from_config
from .provider import S3Provider


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
        self._providers: dict[str, S3Provider] = {}

    def provider_for_cloud(self, cloud: str) -> S3Provider:
        if cloud not in self._providers_by_cloud:
            try:
                config = self._configs[cloud]
            except KeyError as exc:
                raise ValueError(f"No S3 configuration for Juju cloud '{cloud}'; set S3_CONFIG_<cloud>") from exc
            provider = self._provider_factory(config)
            self._providers_by_cloud[cloud] = self._providers.setdefault(provider.handle.resource_id, provider)
        return self._providers_by_cloud[cloud]
