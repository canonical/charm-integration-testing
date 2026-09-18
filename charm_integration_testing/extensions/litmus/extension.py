# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import yaml
from chaos_client.litmus_detection import litmus_is_available
from juju import JujuBackend, JujuConsumedOfferInfo, JujuExtension, JujuModelHandle, JujuVersion
from kubernetes_client import KubernetesBackend
from tenacity import Retrying, retry_if_result, stop_after_delay, wait_fixed

INFRASTRUCTURE_CHARM = "litmus-infrastructure-k8s"
INFRASTRUCTURE_ENDPOINT = "litmus-infrastructure"
CHAOSCENTER_ALIAS = "litmus-chaoscenter"
DEFAULT_LITMUS_CHANNEL = "dev/edge"
DEFAULT_LITMUS_TIMEOUT = timedelta(minutes=10)


@dataclass(frozen=True)
class LitmusConfig:
    offer_url: str
    channel: str = DEFAULT_LITMUS_CHANNEL
    timeout: timedelta = DEFAULT_LITMUS_TIMEOUT

    def __post_init__(self) -> None:
        if (
            any(character.isspace() for character in self.offer_url)
            or JujuConsumedOfferInfo(self.offer_url).parse_url() is None
        ):
            raise ValueError("Litmus offer must use controller:owner/model.offer format.")
        if not self.channel or self.channel != self.channel.strip():
            raise ValueError("Litmus channel must be a nonempty channel name without surrounding whitespace.")
        if self.timeout <= timedelta():
            raise ValueError("Litmus timeout must be positive.")


def infrastructure_bundle(config: LitmusConfig) -> str:
    """Build an infrastructure bundle consuming an existing ChaosCenter offer."""
    return yaml.safe_dump(
        {
            "bundle": "kubernetes",
            "applications": {
                INFRASTRUCTURE_CHARM: {
                    "charm": INFRASTRUCTURE_CHARM,
                    "channel": config.channel,
                    "num_units": 1,
                }
            },
            "saas": {CHAOSCENTER_ALIAS: {"url": config.offer_url}},
            "relations": [
                [f"{INFRASTRUCTURE_CHARM}:{INFRASTRUCTURE_ENDPOINT}", f"{CHAOSCENTER_ALIAS}:{INFRASTRUCTURE_ENDPOINT}"]
            ],
        },
        sort_keys=True,
    )


def infrastructure_is_connected(backend: JujuBackend, model: JujuModelHandle, config: LitmusConfig) -> bool:
    """Check live application, offer and relation identities; reject conflicting names."""
    applications = backend.list_applications(model)
    offers = backend.list_consumed_offers(model)
    if CHAOSCENTER_ALIAS in applications or INFRASTRUCTURE_CHARM in offers:
        raise ValueError(f"Litmus application or offer name is already in use in {model.uri}.")
    for name, info in applications.items():
        if info.charm == INFRASTRUCTURE_CHARM and name != INFRASTRUCTURE_CHARM:
            raise ValueError(f"Litmus infrastructure already exists as {name!r} in {model.uri}.")

    application = applications.get(INFRASTRUCTURE_CHARM)
    if application is not None:
        if application.charm != INFRASTRUCTURE_CHARM:
            raise ValueError(f"Application {INFRASTRUCTURE_CHARM!r} has a different charm in {model.uri}.")
        if application.channel is None or str(application.channel) != config.channel:
            raise ValueError(
                f"Existing Litmus infrastructure channel does not match {config.channel!r} in {model.uri}."
            )

    offer = offers.get(CHAOSCENTER_ALIAS)
    if offer is not None:
        if offer.parse_url() != JujuConsumedOfferInfo(config.offer_url).parse_url():
            raise ValueError(f"Existing {CHAOSCENTER_ALIAS!r} offer does not match the configured URL in {model.uri}.")
        if INFRASTRUCTURE_ENDPOINT not in offer.endpoints:
            raise ValueError(f"Offer {CHAOSCENTER_ALIAS!r} does not expose {INFRASTRUCTURE_ENDPOINT!r}.")
    if application is None or offer is None:
        return False
    return backend.integration_exists(
        INFRASTRUCTURE_CHARM, INFRASTRUCTURE_ENDPOINT, CHAOSCENTER_ALIAS, INFRASTRUCTURE_ENDPOINT, model
    )


def _cluster_uid(backend: KubernetesBackend) -> str:
    uid = backend.core_v1_api.read_namespace("kube-system").metadata.uid
    if not isinstance(uid, str) or not uid:
        raise ValueError("Cannot establish Kubernetes cluster identity from the kube-system namespace.")
    return uid


def validate_litmus_target(backend: JujuBackend, model: JujuModelHandle, config: LitmusConfig) -> KubernetesBackend:
    """Require Juju 3.6+ and the same Kubernetes cluster as the configured ChaosCenter."""
    target = backend.get_kubernetes_client_for_model(model)
    if target is None:
        raise ValueError(f"Litmus was configured for non-Kubernetes model {model.uri}.")
    if backend.version(model) < JujuVersion(3, 6, 0):
        raise ValueError(f"Litmus infrastructure requires Juju 3.6 or newer in {model.uri}.")
    offer = JujuConsumedOfferInfo(config.offer_url).parse_url()
    if offer is None:
        raise ValueError("Litmus offer must use controller:owner/model.offer format.")
    control_plane = backend.get_kubernetes_client_for_model(replace(offer.model, owner=offer.owner))
    if control_plane is None or _cluster_uid(target.backend) != _cluster_uid(control_plane.backend):
        raise ValueError(f"Litmus ChaosCenter and {model.uri} must use the same Kubernetes cluster.")
    return target.backend


def wait_for_litmus(
    backend: KubernetesBackend,
    namespace: str,
    timeout: timedelta,
    *,
    poll_interval: float = 2,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Wait only for absent or unready resources; API errors are not retried here."""
    retrying = Retrying(
        retry=retry_if_result(lambda ready: not ready),
        stop=stop_after_delay(timeout.total_seconds()),
        wait=wait_fixed(poll_interval),
        retry_error_callback=lambda state: False,
    )
    if sleep is not None:
        retrying.sleep = sleep
    ready = retrying(litmus_is_available, backend, namespace)
    if not ready:
        raise TimeoutError(f"Litmus operator in namespace {namespace!r} did not become ready within {timeout}.")


class LitmusExtension(JujuExtension):
    """Connect configured models to Litmus after model creation or deployment."""

    def __init__(self, backend: JujuBackend, configs: dict[JujuModelHandle, LitmusConfig]) -> None:
        self._backend = backend
        self._configs = configs

    def post_add_model(self, controller: str, model: str) -> None:
        self._prepare(JujuModelHandle(controller=controller, model=model))

    def post_deploy(self, model: JujuModelHandle) -> None:
        self._prepare(model)

    def _prepare(self, model: JujuModelHandle) -> None:
        config = self._configs.get(model)
        if config is None:
            return
        kubernetes = validate_litmus_target(self._backend, model, config)
        if not infrastructure_is_connected(self._backend, model, config):
            with TemporaryDirectory(prefix="litmus-bundle-") as directory:
                path = Path(directory) / "bundle.yaml"
                path.write_text(infrastructure_bundle(config), encoding="utf-8")
                # Call the backend, not JujuClient, to avoid re-entering post_deploy.
                self._backend.deploy_bundle_file(model, str(path), timeout=config.timeout)
            if not infrastructure_is_connected(self._backend, model, config):
                raise RuntimeError(f"Litmus infrastructure relation was not established in {model.uri}.")
        wait_for_litmus(kubernetes, model.model, config.timeout)
