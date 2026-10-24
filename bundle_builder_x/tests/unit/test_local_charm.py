# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import stat
from pathlib import Path
from typing import cast
from zipfile import ZipFile, ZipInfo

import pytest
import yaml

from bundle_builder_x import (
    AppSpec,
    BundleBuilder,
    CharmChannel,
    ModelSpec,
    OverridesClient,
    SpecFile,
    unpack_charm_artifact,
)
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmhubHttpClient, RefreshAction, RefreshResponse


def _write_charm(path: Path) -> Path:
    path.mkdir()
    (path / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "local-test",
            }
        ),
        encoding="utf-8",
    )
    (path / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "bases": [
                    {
                        "name": "ubuntu",
                        "channel": "26.04",
                        "architectures": ["amd64"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (path / "config.yaml").write_text("options: {}\n", encoding="utf-8")
    return path


class TestLocalCharm:
    def test_bundle_builder_uses_local_metadata_and_artifact_path(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")
        spec = SpecFile(
            models=[
                ModelSpec(
                    name="local-model",
                    platform="machine",
                    juju="4.0.0",
                    applications={
                        "local-test": AppSpec(
                            charm="local-test",
                            local_charm=charm_path,
                            channel="2/edge",
                            revision=42,
                        )
                    },
                )
            ]
        )

        solution = BundleBuilder(CharmhubClient()).build(spec)

        bundle_app = solution.bundles[0].applications["local-test"]
        assert bundle_app.charm.source_path == charm_path.resolve()
        assert bundle_app.charm.channel == CharmChannel.model_validate("2/edge")
        assert bundle_app.charm.revision == 42
        assert bundle_app.charm.ubuntu_version == "26.04"
        exported = yaml.safe_load(solution.bundles[0].export())
        assert exported["applications"]["local-test"]["charm"] == str(charm_path.resolve())
        assert exported["applications"]["local-test"]["base"] == "ubuntu@26.04"
        assert "channel" not in exported["applications"]["local-test"]
        assert "revision" not in exported["applications"]["local-test"]

    def test_local_charm_rejects_unavailable_base(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")

        with pytest.raises(ValueError, match="does not support requested base"):
            CharmhubClient().charm_from_local(
                charm_path=charm_path,
                charm_name="local-test",
                ubuntu_arch="amd64",
                platform="machine",
                ubuntu_version="24.04",
            )

    def test_local_charm_uses_runtime_bases_from_nested_manifest(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")
        (charm_path / "manifest.yaml").write_text(
            yaml.safe_dump(
                {
                    "bases": [
                        {
                            "build-on": [{"name": "ubuntu", "channel": "24.04", "architectures": ["amd64"]}],
                            "run-on": [{"name": "ubuntu", "channel": "26.04", "architectures": ["amd64"]}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        charm = CharmhubClient().charm_from_local(
            charm_path=charm_path,
            charm_name="local-test",
            ubuntu_arch="amd64",
            platform="machine",
            ubuntu_version="26.04",
            channel=CharmChannel.model_validate("latest/stable"),
            revision=1,
        )

        assert charm.ubuntu_version == "26.04"

    def test_local_charm_uses_test_channel_for_policy_overrides(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")
        overrides_path = tmp_path / "overrides"
        overrides_path.mkdir()
        (overrides_path / "local-test.yaml").write_text(
            "default_channel: '3.0/stable'\n"
            "default_revision: 9\n"
            "overrides:\n"
            "  - criteria:\n"
            "      - track: '1'\n"
            "    ha_units: 4\n"
            "  - criteria:\n"
            "      - track: '2'\n"
            "    ha_units: 5\n",
            encoding="utf-8",
        )
        client = CharmhubClient(overrides_client=OverridesClient(overrides=overrides_path))

        charm = client.charm_from_local(
            charm_path=charm_path,
            charm_name="local-test",
            ubuntu_arch="amd64",
            channel=CharmChannel.model_validate("2/edge"),
            revision=42,
            platform="machine",
        )

        assert charm.channel == CharmChannel.model_validate("2/edge")
        assert charm.revision == 42
        assert charm.ha_units == 5

    def test_local_charm_resolves_override_default_release_context(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")
        overrides_path = tmp_path / "overrides"
        overrides_path.mkdir()
        (overrides_path / "local-test.yaml").write_text(
            "default_channel: '3.0/stable'\n"
            "default_revision: 9\n"
            "overrides:\n"
            "  - criteria:\n"
            "      - track: '3.0'\n"
            "    ha_units: 4\n"
            "  - criteria:\n"
            "      - ubuntu_version: '26.04'\n"
            "    ha_units: 6\n",
            encoding="utf-8",
        )

        charm = CharmhubClient(overrides_client=OverridesClient(overrides=overrides_path)).charm_from_local(
            charm_path=charm_path,
            charm_name="local-test",
            ubuntu_arch="amd64",
            platform="machine",
        )

        assert charm.channel == CharmChannel.model_validate("3.0/stable")
        assert charm.revision == 9
        assert charm.ha_units == 4

    def test_local_charm_resolves_default_context_from_charmhub(self, tmp_path: Path) -> None:
        class _DefaultReleaseClient:
            def refresh(self, action: RefreshAction) -> RefreshResponse:
                return RefreshResponse(
                    name="local-test",
                    effective_channel="4.0/edge",
                    charm=RefreshResponse.Charm(revision=27),
                )

        charm = CharmhubClient(
            http_client=cast(CharmhubHttpClient, _DefaultReleaseClient()),
        ).charm_from_local(
            charm_path=_write_charm(tmp_path / "local-test"),
            charm_name="local-test",
            ubuntu_arch="amd64",
            platform="machine",
        )

        assert charm.channel == CharmChannel.model_validate("4.0/edge")
        assert charm.revision == 27

    def test_local_charm_rejects_malformed_manifest(self, tmp_path: Path) -> None:
        charm_path = _write_charm(tmp_path / "local-test")
        (charm_path / "manifest.yaml").write_text("bases: invalid\n", encoding="utf-8")

        with pytest.raises(ValueError, match="manifest must contain a list of bases"):
            CharmhubClient().charm_from_local(
                charm_path=charm_path,
                charm_name="local-test",
                ubuntu_arch="amd64",
                platform="machine",
            )

    def test_unpack_rejects_archive_path_traversal(self, tmp_path: Path) -> None:
        artifact = tmp_path / "unsafe.charm"
        with ZipFile(artifact, "w") as archive:
            archive.writestr("../metadata.yaml", "name: unsafe\n")

        with pytest.raises(ValueError, match="unsafe path"):
            unpack_charm_artifact(artifact, tmp_path / "unpacked")

    def test_unpack_rejects_symbolic_links(self, tmp_path: Path) -> None:
        artifact = tmp_path / "symlink.charm"
        symlink = ZipInfo("dispatch")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with ZipFile(artifact, "w") as archive:
            archive.writestr(symlink, "target")

        with pytest.raises(ValueError, match="symbolic link"):
            unpack_charm_artifact(artifact, tmp_path / "unpacked")

    def test_unpack_charmcraft_artifact(self, tmp_path: Path) -> None:
        source = _write_charm(tmp_path / "source")
        executable = source / "dispatch"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        artifact = tmp_path / "local-test.charm"
        with ZipFile(artifact, "w") as archive:
            for file_path in source.iterdir():
                archive.write(file_path, file_path.name)

        unpacked = unpack_charm_artifact(artifact, tmp_path / "unpacked")

        assert (unpacked / "metadata.yaml").is_file()
        assert (unpacked / "dispatch").stat().st_mode & 0o111

    def test_unpack_rejects_invalid_archive(self, tmp_path: Path) -> None:
        artifact = tmp_path / "invalid.charm"
        artifact.write_text("not a zip file", encoding="utf-8")

        with pytest.raises(ValueError, match="Invalid charm artifact archive"):
            unpack_charm_artifact(artifact, tmp_path / "unpacked")
