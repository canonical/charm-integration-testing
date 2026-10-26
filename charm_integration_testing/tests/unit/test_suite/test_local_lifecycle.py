# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from pathlib import Path

import yaml
from juju import JujuApplicationInfo, JujuClient, JujuModelHandle
from test_suite.test_deploy_target_old_revision import _create_bundle_with_revision_override
from test_suite.test_downgrade_charm import test_downgrade_charm as downgrade
from test_suite.test_upgrade_charm import test_upgrade_charm as upgrade

from bundle_builder_x import Charm, CharmChannel


class LifecycleClient(JujuClient):
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.local_refreshes: list[Path] = []
        self.store_refreshes: list[tuple[str, int]] = []
        self.revision = 7
        self.local = True

    def refresh_application_from_local(
        self, application: str, model: JujuModelHandle, path: Path, resources: dict[str, str]
    ) -> None:
        self.local_refreshes.append(path)
        self.revision = 8
        self.local = True

    def refresh_application_from_store(
        self, application: str, model: JujuModelHandle, charm: str, revision: int, channel: str | None
    ) -> None:
        self.store_refreshes.append((charm, revision))
        self.revision = revision
        self.local = False

    def wait_for_application_revision(
        self, application: str, expected_revision: int, timeout: timedelta | None, model: JujuModelHandle
    ) -> None:
        assert self.revision == expected_revision

    def multi_model_idle_for_period(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None = None,
        count: int = 3,
        strict_timeout: bool = False,
    ) -> None:
        pass

    def application_revision(self, application: str, model: JujuModelHandle) -> int:
        return self.revision

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return {"target": JujuApplicationInfo(charm="local-db", revision=self.revision, local=self.local)}

    def validate_model(
        self, model: JujuModelHandle, level: str | None = None, *, applications: list[str] | None = None
    ) -> None:
        pass


def test_local_charm_returns_to_built_artifact_after_downgrade(tmp_path: Path) -> None:
    # GIVEN a local target and an explicitly selected historical store revision
    path = tmp_path / "local.charm"
    local = Charm(
        name="local-db",
        local_path=str(path),
        revision=-1,
        channel=CharmChannel.model_validate("latest/stable"),
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        platforms=["kubernetes"],
        endpoints={},
    )
    client = LifecycleClient()
    model = JujuModelHandle(controller="controller", model="model")

    # WHEN the suite downgrades and then upgrades the target
    downgrade(client, 42, model, None, "local-db", "target", None, "latest/stable", local)
    upgrade(client, 42, model, None, "target", "latest/stable", local)

    # THEN upgrade loads the local file, identified by origin since Juju numbers local revisions from 0
    assert client.store_refreshes == [("local-db", 42)]
    assert client.local_refreshes == [path]
    assert client.local


def test_historical_deployment_replaces_local_file_selector(tmp_path: Path) -> None:
    source = tmp_path / "bundle.yaml"
    source.write_text("applications:\n  target:\n    charm: /tmp/local.charm\n    resources:\n      image: db:1\n")
    destination = tmp_path / "historical.yaml"
    _create_bundle_with_revision_override(source, destination, "target", 42, "local-db", "latest/stable")
    app = yaml.safe_load(destination.read_text())["applications"]["target"]
    assert app["charm"] == "local-db"
    assert app["revision"] == 42
    assert app["channel"] == "latest/stable"
    assert app["resources"] == {"image": "db:1"}
