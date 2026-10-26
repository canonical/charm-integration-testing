# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from functools import cache
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from bundle_builder_x.bundle import Application, Bundle
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmhubHttpClient, RefreshAction, RefreshResponse
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.release_errors import ArchitectureMismatchError, BaseMismatchError, PlatformMismatchError


class StoreStub(CharmhubHttpClient):
    @cache
    def refresh(self, action: RefreshAction) -> RefreshResponse:
        raise RuntimeError(f"store revision requested: {action.charm_revision}")


def archive(path: Path, resources: dict[str, object] | None = None) -> Path:
    with ZipFile(path, "w") as charm:
        charm.writestr(
            "metadata.yaml",
            yaml.safe_dump(
                {
                    "name": "local-db",
                    "containers": {"db": {"resource": "image"}},
                    "provides": {"database": {"interface": "db"}},
                    "resources": resources or {},
                }
            ),
        )
        charm.writestr(
            "manifest.yaml",
            yaml.safe_dump({"bases": [{"name": "ubuntu", "channel": "24.04", "architectures": ["amd64"]}]}),
        )
        charm.writestr("config.yaml", "options:\n  port:\n    type: int\n    default: 5432\n")
    return path


def test_local_archive_metadata_and_export(tmp_path: Path) -> None:
    # GIVEN a local charm that does not need a Charmhub release
    path = archive(tmp_path / "local-db.charm", {"image": {"type": "oci-image"}})
    client = CharmhubClient(http_client=StoreStub())

    # WHEN resolving and exporting a deployment bundle
    local = client.register_local_charm(path, "amd64", resources={"image": "registry.example/db:1"})
    resolved = client.charm_from_store("local-db", "amd64", platform="kubernetes")
    bundle = Bundle(
        applications={"target": Application(charm=resolved, resources={"image": "registry.example/db:1"})},
        integrations=set(),
        platform="kubernetes",
        arch="amd64",
        juju_version=JujuVersion(major=3, minor=6, patch=0),
    )
    exported = yaml.safe_load(bundle.export())["applications"]["target"]

    # THEN local metadata and the absolute artifact survive resolution without store selectors
    assert resolved is local
    assert client.registered_local_charm("local-db") is local
    assert resolved.config_defaults == {"port": 5432}
    assert resolved.endpoints["database"].interface == "db"
    assert exported["charm"] == str(path)
    assert exported["resources"] == {"image": "registry.example/db:1"}
    assert "revision" not in exported
    assert "channel" not in exported


def test_historical_revision_still_resolves_from_store(tmp_path: Path) -> None:
    client = CharmhubClient(http_client=StoreStub())
    client.register_local_charm(archive(tmp_path / "test.charm"), "amd64")
    with pytest.raises(RuntimeError, match="store revision requested: 42"):
        client.charm_from_store("local-db", "amd64", charm_revision=42)


@pytest.mark.parametrize(("arch", "base"), [("arm64", None), ("amd64", "22.04")], ids=["wrong-arch", "wrong-base"])
def test_archive_manifest_mismatch(tmp_path: Path, arch: str, base: str | None) -> None:
    with pytest.raises(ValueError, match="does not support"):
        CharmhubClient().register_local_charm(archive(tmp_path / "test.charm"), arch, base)


def test_local_resolution_checks_environment(tmp_path: Path) -> None:
    client = CharmhubClient()
    client.register_local_charm(archive(tmp_path / "test.charm"), "amd64")
    with pytest.raises(ArchitectureMismatchError):
        client.charm_from_store("local-db", "arm64")
    with pytest.raises(BaseMismatchError):
        client.charm_from_store("local-db", "amd64", ubuntu_version="22.04")
    with pytest.raises(PlatformMismatchError):
        client.charm_from_store("local-db", "amd64", platform="machine")


def test_local_resources_must_be_explicit(tmp_path: Path) -> None:
    path = archive(tmp_path / "test.charm", {"image": {"type": "oci-image"}})
    with pytest.raises(ValueError, match="resources must match"):
        CharmhubClient().register_local_charm(path, "amd64")
    with pytest.raises(ValueError, match="must not be empty"):
        CharmhubClient().register_local_charm(path, "amd64", resources={"image": ""})


def test_missing_file_resource_fails_before_deployment(tmp_path: Path) -> None:
    path = archive(tmp_path / "test.charm", {"payload": {"type": "file"}})
    with pytest.raises(ValueError, match="does not exist"):
        CharmhubClient().register_local_charm(path, "amd64", resources={"payload": str(tmp_path / "absent")})


def test_oci_resources_default_to_upstream_source(tmp_path: Path) -> None:
    # GIVEN a local charm whose image resource declares an upstream source
    path = archive(tmp_path / "test.charm", {"image": {"type": "oci-image", "upstream-source": "db:2"}})

    # WHEN/THEN the declared image is used unless the caller overrides it
    assert CharmhubClient().register_local_charm(path, "amd64").resources == {"image": ["db:2"]}
    overridden = CharmhubClient().register_local_charm(path, "amd64", resources={"image": "db:3"})
    assert overridden.resources == {"image": ["db:3"]}
