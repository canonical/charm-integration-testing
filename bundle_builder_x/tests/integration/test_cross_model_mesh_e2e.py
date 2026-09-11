# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-to-end reproduction of the cross_model_mesh companion-constraint behavior (#980)
against the real Charmhub API, using blackbox-exporter-k8s (dev/edge) as a real charm
that exposes provide-cmr-mesh/require-cmr-mesh/self-metrics-endpoint.

The override used here is a minimal, self-contained copy of the constraint shape
landed for real charms in #1021 - kept local so this test does not depend on that
PR having merged.
"""

from pathlib import Path

import pytest
import yaml

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.overrides import OverridesClient
from bundle_builder_x.snapstore import SnapstoreClient
from bundle_builder_x.spec import SpecFile

_OVERRIDE_YAML = """\
---
overrides:
  - criteria:
      - track: dev
    constraints:
      - 'charms(cross_model(endpoint[self-metrics-endpoint])) == charms(cross_model(endpoint[provide-cmr-mesh]))'
      - 'len(cross_model(endpoint[self-metrics-endpoint])) == len(cross_model(endpoint[provide-cmr-mesh]))'
      - 'len(endpoint[provide-cmr-mesh]) == len(cross_model(endpoint[provide-cmr-mesh]))'
"""


@pytest.fixture
def mesh_charmhub_client(tmp_path: Path, snapstore_client: SnapstoreClient) -> CharmhubClient:
    (tmp_path / "blackbox-exporter-k8s.yaml").write_text(_OVERRIDE_YAML)
    overrides_client = OverridesClient(overrides=tmp_path)
    return CharmhubClient(overrides_client=overrides_client)


def test_purely_local_mesh_companion_is_rejected(
    mesh_charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN two real blackbox-exporter-k8s applications with provide-cmr-mesh/require-cmr-mesh
    # related purely locally (no other cross-model relation between them at all)
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {
                        "exporter1": {"charm": "blackbox-exporter-k8s", "channel": "dev/edge"},
                        "exporter2": {"charm": "blackbox-exporter-k8s", "channel": "dev/edge"},
                    },
                    "integrations": [
                        {
                            "application": "exporter1",
                            "endpoint": "provide-cmr-mesh",
                            "remote_application": "exporter2",
                            "remote_endpoint": "require-cmr-mesh",
                        },
                    ],
                }
            ]
        }
    )
    builder = BundleBuilder(charmhub_client=mesh_charmhub_client, snapstore_client=snapstore_client)

    # THEN building fails: cross_model_mesh only makes sense alongside a genuine CMR (#980)
    with pytest.raises(UncompletableBundleError):
        builder.build(spec)


def test_matching_external_cmr_mesh_companion_is_accepted_and_shares_one_offer(
    mesh_charmhub_client: CharmhubClient,
    snapstore_client: SnapstoreClient,
) -> None:
    # GIVEN self-metrics-endpoint and provide-cmr-mesh both integrated cross-model with the
    # same external application/model (a genuine CMR mesh pairing)
    spec = SpecFile.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "platform": "kubernetes",
                    "applications": {
                        "exporter1": {"charm": "blackbox-exporter-k8s", "channel": "dev/edge"},
                    },
                    "integrations": [
                        {
                            "application": "exporter1",
                            "endpoint": "self-metrics-endpoint",
                            "remote_application": "scraper",
                            "remote_endpoint": "metrics-endpoint",
                            "remote_model": "external-model",
                            "url": "admin/external-model.scraper",
                        },
                        {
                            "application": "exporter1",
                            "endpoint": "provide-cmr-mesh",
                            "remote_application": "scraper",
                            "remote_endpoint": "require-cmr-mesh",
                            "remote_model": "external-model",
                            "url": "admin/external-model.scraper",
                        },
                    ],
                }
            ]
        }
    )
    builder = BundleBuilder(charmhub_client=mesh_charmhub_client, snapstore_client=snapstore_client)

    # WHEN building
    solution = builder.build(spec)

    # THEN it succeeds, and the two cross-model relations share exactly one Juju offer
    # (the offer-sharing generalization this PR adds)
    bundle = solution.bundles[0]
    docs = [d for d in yaml.safe_load_all(bundle.export()) if isinstance(d, dict)]
    offers_doc = next(d for d in docs if "applications" in d and "offers" in d["applications"].get("exporter1", {}))
    offers = offers_doc["applications"]["exporter1"]["offers"]
    assert len(offers) == 1
    (offer_endpoints,) = offers.values()
    assert set(offer_endpoints["endpoints"]) == {"provide-cmr-mesh", "self-metrics-endpoint"}
