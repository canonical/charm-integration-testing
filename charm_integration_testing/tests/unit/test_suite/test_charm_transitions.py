# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from juju import JujuApplicationInfo, JujuClient, JujuModelHandle
from test_suite.charm_transitions import deploy_bundle_with_charm_and_validate, refresh_and_validate

from bundle_builder_x import Charm, CharmChannel

MODEL = JujuModelHandle(controller="controller", model="model")


class JujuClientStub:
    def __init__(self, infos: list[JujuApplicationInfo]) -> None:
        self.infos = infos
        self.logger = logging.getLogger("test")
        self.calls: list[tuple[Any, ...]] = []
        self.deployed_bundle_documents: list[list[Any]] = []

    def application_info(self, application: str, model: JujuModelHandle) -> JujuApplicationInfo:
        return self.infos.pop(0)

    def refresh_application(self, application: str, model: JujuModelHandle, revision: int, channel: str) -> None:
        self.calls.append(("refresh", application, revision, channel))

    def refresh_application_from_path(self, application: str, path: Path, model: JujuModelHandle) -> None:
        self.calls.append(("refresh_path", application, path))

    def wait_for_application_revision(self, **kwargs: Any) -> None:
        self.calls.append(("wait_revision", kwargs["expected_revision"]))

    def deploy_bundle_file(self, bundle: str, model: JujuModelHandle) -> None:
        documents = list(yaml.safe_load_all(Path(bundle).read_text(encoding="utf-8")))
        self.deployed_bundle_documents.append(documents)
        self.calls.append(("deploy", documents[0]))

    def multi_model_idle_for_period(self, models: list[JujuModelHandle], timeout: timedelta) -> None:
        self.calls.append(("idle",))

    def validate_model(self, model: JujuModelHandle, level: str) -> None:
        self.calls.append(("validate",))


def _charmhub_charm(revision: int = 7) -> Charm:
    return Charm(
        name="my-charm",
        channel=CharmChannel.model_validate("1/stable"),
        revision=revision,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )


def _local_charm(path: Path) -> Charm:
    return Charm(
        name="my-charm",
        source_path=path,
        channel=CharmChannel.model_validate("1/stable"),
        revision=1,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )


def _refresh(client: JujuClientStub, charm: Charm) -> None:
    refresh_and_validate(cast(JujuClient, client), "app", charm, MODEL, None)


def test_refreshes_to_local_charm_and_requires_new_local_revision() -> None:
    # GIVEN an application moving from Charmhub to a new local upload
    client = JujuClientStub(
        [
            JujuApplicationInfo(charm="my-charm", revision=7, origin="charmhub"),
            JujuApplicationInfo(charm="local:my-charm-0", revision=0, origin="local"),
        ]
    )

    # WHEN
    _refresh(client, _local_charm(Path("/charms/my-charm")))

    # THEN the charm is refreshed by path and the models are validated
    assert client.calls == [("refresh_path", "app", Path("/charms/my-charm")), ("idle",), ("validate",)]


def test_fails_when_local_refresh_leaves_previous_local_upload() -> None:
    # GIVEN Juju still reports the local revision that was running before the refresh
    client = JujuClientStub(
        [
            JujuApplicationInfo(charm="local:my-charm-1", revision=1, origin="local"),
            JujuApplicationInfo(charm="local:my-charm-1", revision=1, origin="local"),
        ]
    )

    # WHEN / THEN
    with pytest.raises(pytest.fail.Exception, match="newly uploaded local charm"):
        _refresh(client, _local_charm(Path("/charms/my-charm")))


def test_refreshes_to_charmhub_revision_and_verifies_origin() -> None:
    # GIVEN an application moving from a local upload back to Charmhub
    client = JujuClientStub(
        [
            JujuApplicationInfo(charm="local:my-charm-0", revision=0, origin="local"),
            JujuApplicationInfo(charm="my-charm", revision=7, origin="charmhub"),
        ]
    )

    # WHEN
    _refresh(client, _charmhub_charm(revision=7))

    # THEN the exact Charmhub revision is requested and awaited
    assert client.calls[:2] == [("refresh", "app", 7, "1/stable"), ("wait_revision", 7)]


def test_fails_when_charmhub_revision_matches_but_origin_is_local() -> None:
    # GIVEN a local upload whose Juju revision collides with the Charmhub revision
    client = JujuClientStub(
        [
            JujuApplicationInfo(charm="my-charm", revision=6, origin="charmhub"),
            JujuApplicationInfo(charm="local:my-charm-7", revision=7, origin="local"),
        ]
    )

    # WHEN / THEN
    with pytest.raises(pytest.fail.Exception, match="my-charm revision 7"):
        _refresh(client, _charmhub_charm(revision=7))


def test_deploys_bundle_with_application_switched_to_local_charm(tmp_path: Path) -> None:
    # GIVEN a bundle pinning the application to a Charmhub revision
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        yaml.safe_dump({"applications": {"app": {"charm": "my-charm", "channel": "1/stable", "revision": 7}}}),
        encoding="utf-8",
    )
    client = JujuClientStub([JujuApplicationInfo(charm="local:my-charm-0", revision=0, origin="local")])

    # WHEN deploying it with the application switched to a local charm
    deploy_bundle_with_charm_and_validate(
        cast(JujuClient, client),
        bundle=bundle,
        destination_bundle=tmp_path / "deploy.yaml",
        application="app",
        charm=_local_charm(Path("/charms/my-charm")),
        target_model=MODEL,
        neighbor_model=None,
    )

    # THEN the deployed bundle selects the local path without Charmhub release fields
    assert client.calls[0] == ("deploy", {"applications": {"app": {"charm": "/charms/my-charm"}}})


def test_deploys_bundle_with_local_charm_without_dropping_offer_overlay(tmp_path: Path) -> None:
    # GIVEN a multi-document bundle with a provider offer in its overlay
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        yaml.safe_dump_all(
            [
                {
                    "applications": {
                        "app": {
                            "charm": "my-charm",
                            "channel": "1/stable",
                            "revision": 7,
                        }
                    }
                },
                {
                    "applications": {
                        "app": {
                            "offers": {
                                "app-offer": {
                                    "endpoints": ["database"],
                                }
                            }
                        }
                    }
                },
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    client = JujuClientStub([JujuApplicationInfo(charm="local:my-charm-0", revision=0, origin="local")])

    # WHEN the application is switched to a local charm
    deploy_bundle_with_charm_and_validate(
        cast(JujuClient, client),
        bundle=bundle,
        destination_bundle=tmp_path / "deploy.yaml",
        application="app",
        charm=_local_charm(Path("/charms/my-charm")),
        target_model=MODEL,
        neighbor_model=None,
    )

    # THEN the base selects the local charm and the provider-offer overlay is preserved
    base, overlay = client.deployed_bundle_documents[0]
    assert base["applications"]["app"] == {"charm": "/charms/my-charm"}
    assert overlay == {
        "applications": {
            "app": {
                "offers": {
                    "app-offer": {
                        "endpoints": ["database"],
                    }
                }
            }
        }
    }
