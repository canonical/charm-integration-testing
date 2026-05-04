# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from juju import JujuClient

from .scheduler.states import State


def _strip_saas_from_bundle(bundle_yaml: str) -> str:
    """Return bundle YAML with the saas section removed and any cross-model relations filtered out.

    This is used in the first deployment phase so that all models' applications and offers are
    created before any model attempts to consume a remote offer. Without this, bidirectional CMR
    (where both models consume from each other) would deadlock: neither can be deployed first.

    Handles multi-document YAML (base bundle + overlay) produced when the bundle contains offers.
    """
    documents = list(yaml.safe_load_all(bundle_yaml))
    base = documents[0]
    saas_names = set(base.pop("saas", {}).keys())
    if saas_names:
        # Cross-model relations reference the saas alias on one side; filter those out.
        base["relations"] = [
            rel for rel in base.get("relations", []) if not any(ep.split(":")[0] in saas_names for ep in rel)
        ]
    parts = [yaml.dump(base, default_flow_style=False, sort_keys=True)]
    # Preserve any overlay documents (e.g. offers) unchanged.
    for doc in documents[1:]:
        parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=True))
    return "---\n" + "---\n".join(parts) if len(parts) > 1 else parts[0]


@pytest.mark.state(requires=State.EMPTY_MODEL, provides=State.DEPLOYED)
def test_deploy(
    juju_client: JujuClient,
    target_bundle: Path,
    neighbor_bundle: Path | None,
    model: str,
    target_controller: str,
    neighbor_model: str | None,
    neighbor_controller: str | None,
    tmp_path: Path,
) -> None:
    target_model_uri = f"{target_controller}:{model}"
    all_bundles: list[tuple[Path, str]] = [(target_bundle, target_model_uri)]
    if neighbor_bundle is not None:
        assert neighbor_controller is not None
        assert neighbor_model is not None
        all_bundles.append((neighbor_bundle, f"{neighbor_controller}:{neighbor_model}"))

    # Phase 1: Deploy applications and offers for all models, without consuming remote offers.
    #          Each bundle's saas section (and the cross-model relations that reference it) is
    #          stripped so the deploy succeeds even when the remote offer does not exist yet.
    #          After this phase every offer exists and all local applications are running.
    #          For single-model bundles there is no saas section, so this is equivalent to
    #          a normal deploy.
    for i, (bundle_path, model_uri) in enumerate(all_bundles):
        apps_only_yaml = _strip_saas_from_bundle(bundle_path.read_text(encoding="utf-8"))
        apps_only_path = tmp_path / f"apps-only-bundle-{i}.yaml"
        apps_only_path.write_text(apps_only_yaml, encoding="utf-8")
        juju_client.deploy_bundle_file(str(apps_only_path), model=model_uri)

    # Phase 2: Re-deploy the full bundles so that remote offers are consumed and cross-model
    #          relations are established. All offers now exist from phase 1 so both sides can
    #          complete their saas consumption regardless of order.
    #          For single-model bundles this re-deploy is idempotent.
    for bundle_path, model_uri in all_bundles:
        juju_client.deploy_bundle_file(str(bundle_path), model=model_uri)

    # TODO: Add multi-model wait
    # https://github.com/canonical/charm-integration-testing/issues/515
    for _, model_uri in all_bundles:
        juju_client.idle_for_period(model=model_uri, timeout=timedelta(minutes=15))

    for _, model_uri in all_bundles:
        juju_client.validate_model(model=model_uri, level="deep")
