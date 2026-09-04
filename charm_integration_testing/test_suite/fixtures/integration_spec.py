# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from typing import Literal

import pytest
import yaml
from juju import JujuApplicationInfo, JujuClient, JujuIntegrationApplication, JujuModelHandle

from bundle_builder_x import CharmChannel, OverridesClient


def _find_saas_alias(bundle_path: Path, local_app: str, local_ep: str, remote_ep: str) -> str | None:
    """Return the SAAS alias for a cross-model relation in the bundle, or ``None``.

    Looks for a relation where ``local_app:local_ep`` is one side and the other side is a ``saas``
    entry whose endpoint is ``remote_ep``.

    Bundle YAML relations may be a flat list of strings (``["app:ep", "app2:ep2"]``) or a nested
    list of single-item lists (``[["app:ep"], ["app2:ep2"]]``); both forms are normalised here.

    Bundles can be multi-document YAML (base bundle + offers overlay), so we use safe_load_all()
    and read only the first document (where SAAS entries always reside).
    """
    try:
        data = next(yaml.safe_load_all(bundle_path.read_text(encoding="utf-8")))
    except StopIteration:
        # Empty file
        return None
    if not isinstance(data, dict):
        # Invalid or non-dict YAML document
        return None
    saas_names = set(data.get("saas", {}).keys())
    if not saas_names:
        return None
    for relation in data.get("relations", []):
        # Normalise nested-list form: [["app:ep"], ["app2:ep2"]] → ["app:ep", "app2:ep2"]
        endpoints = [side[0] if isinstance(side, list) else side for side in relation]
        parts = [ep.split(":", 1) for ep in endpoints]
        for i, (app, ep) in enumerate(parts):
            if app == local_app and ep == local_ep:
                other_parts = parts[1 - i]
                other_app: str = other_parts[0]
                other_ep = other_parts[1]
                if other_app in saas_names and other_ep == remote_ep:
                    return other_app
    return None


@pytest.fixture
def _integration_consuming_side(
    target_bundle: Path,
    neighbor_bundle: Path | None,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
    neighbor_controller: str | None,
    neighbor_model: str | None,
) -> Literal["target", "neighbor", "same"]:
    """Which model holds the consuming side of the integration.

    Returns ``"target"`` when the target bundle has a ``saas`` section for the neighbor offer,
    ``"neighbor"`` when the neighbor bundle has a ``saas`` section for the target offer, and
    ``"same"`` for non-CMR (same-model) integrations.
    """
    if _find_saas_alias(target_bundle, target_application, target_endpoint, neighbor_endpoint) is not None:
        return "target"
    if neighbor_bundle is not None and neighbor_controller is not None and neighbor_model is not None:
        if _find_saas_alias(neighbor_bundle, neighbor_application, neighbor_endpoint, target_endpoint) is not None:
            return "neighbor"
    return "same"


@pytest.fixture
def integration_controller(
    _integration_consuming_side: Literal["target", "neighbor", "same"],
    target_controller: str,
    neighbor_controller: str | None,
) -> str:
    """Controller that owns the integration (target or neighbor, depending on which side consumes)."""
    if _integration_consuming_side == "neighbor":
        assert neighbor_controller is not None
        return neighbor_controller
    return target_controller


@pytest.fixture
def integration_model(
    _integration_consuming_side: Literal["target", "neighbor", "same"],
    model: str,
    neighbor_model: str | None,
) -> str:
    """Model name that owns the integration (target or neighbor, depending on which side consumes)."""
    if _integration_consuming_side == "neighbor":
        assert neighbor_model is not None
        return neighbor_model
    return model


@pytest.fixture
def integration_model_ref(integration_controller: str, integration_model: str) -> JujuModelHandle:
    """Explicit controller+model reference for the model that owns the integration."""
    return JujuModelHandle(controller=integration_controller, model=integration_model)


@pytest.fixture
def integration_endpoint_1(
    _integration_consuming_side: Literal["target", "neighbor", "same"],
    neighbor_bundle: Path | None,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
) -> JujuIntegrationApplication:
    """First endpoint of the integration, resolved for CMR.

    For same-model or target-as-consumer integrations this is ``(target_application, target_endpoint)``.
    When the neighbor model is the consuming side, this is the SAAS alias the neighbor created for
    the target offer, paired with ``target_endpoint``.
    """
    if _integration_consuming_side == "neighbor":
        assert neighbor_bundle is not None
        saas_alias = _find_saas_alias(neighbor_bundle, neighbor_application, neighbor_endpoint, target_endpoint)
        assert saas_alias is not None
        return JujuIntegrationApplication(saas_alias, target_endpoint)
    return JujuIntegrationApplication(target_application, target_endpoint)


@pytest.fixture
def integration_endpoint_2(
    _integration_consuming_side: Literal["target", "neighbor", "same"],
    target_bundle: Path,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
) -> JujuIntegrationApplication:
    """Second endpoint of the integration, resolved for CMR.

    For same-model or neighbor-as-consumer integrations this is ``(neighbor_application, neighbor_endpoint)``.
    When the target model is the consuming side, this is the SAAS alias the target created for
    the neighbor offer, paired with ``neighbor_endpoint``.
    """
    if _integration_consuming_side == "target":
        saas_alias = _find_saas_alias(target_bundle, target_application, target_endpoint, neighbor_endpoint)
        assert saas_alias is not None
        return JujuIntegrationApplication(saas_alias, neighbor_endpoint)
    return JujuIntegrationApplication(neighbor_application, neighbor_endpoint)


@pytest.fixture
def integration_endpoints_removable(
    charm_overrides: Path,
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
    logger: logging.Logger,
) -> bool:
    """Whether both sides of the tested integration allow remove-and-restore testing.

    Resolved from the live models so it reflects whichever charm/channel is actually deployed.
    """
    overrides_client = OverridesClient(overrides=charm_overrides, logger=logger)
    # Non-CMR tests have no neighbor model; the neighbor application lives in target_model_ref.
    # Cache by model so a non-CMR run only calls list_applications once for the shared model.
    applications_by_model: dict[JujuModelHandle, dict[str, JujuApplicationInfo]] = {}
    for model_ref, application, endpoint in (
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref or target_model_ref, neighbor_application, neighbor_endpoint),
    ):
        if model_ref not in applications_by_model:
            applications_by_model[model_ref] = juju_client.list_applications(model=model_ref)
        info = applications_by_model[model_ref].get(application)
        if info is None or info.channel is None:
            continue
        channel = CharmChannel.model_validate(str(info.channel))
        if not overrides_client.get_charm_endpoint_removable(info.charm, channel, endpoint):
            return False
    return True
