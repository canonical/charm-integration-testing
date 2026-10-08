# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from zipfile import ZipFile

import pytest
from juju import JujuModelHandle
from test_suite import conftest

from bundle_builder_x import unpack_charm_artifact


def _write_charm_artifact(path: Path) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("metadata.yaml", "name: test-charm\n")
    return path


def test_snap_accessible_cache_is_scoped_to_project_mount(tmp_path: Path) -> None:
    artifact = _write_charm_artifact(tmp_path / "test-charm.charm")
    snap_common = tmp_path / "snap" / "juju" / "common"
    first_project = tmp_path / "mount-a" / "project"
    second_project = tmp_path / "mount-b" / "project"

    first_path = conftest._unpack_local_charm_artifact(artifact, first_project, snap_common)
    second_path = conftest._unpack_local_charm_artifact(artifact, second_project, snap_common)

    assert first_path != second_path
    assert first_path.is_relative_to(snap_common / "charm-artifacts")
    assert second_path.is_relative_to(snap_common / "charm-artifacts")
    assert first_path.joinpath("metadata.yaml").read_text(encoding="utf-8") == "name: test-charm\n"
    assert second_path.joinpath("metadata.yaml").read_text(encoding="utf-8") == "name: test-charm\n"


def test_project_cache_reuses_extracted_artifact_across_invocations(tmp_path: Path) -> None:
    artifact = _write_charm_artifact(tmp_path / "test-charm.charm")
    project_root = tmp_path / "project"
    snap_common = tmp_path / "snap" / "juju" / "common"

    first_path = conftest._unpack_local_charm_artifact(artifact, project_root, snap_common)
    second_path = conftest._unpack_local_charm_artifact(artifact, project_root, snap_common)

    assert second_path == first_path


def test_concurrent_agents_can_unpack_the_same_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _write_charm_artifact(tmp_path / "test-charm.charm")
    project_root = tmp_path / "project"
    snap_common = tmp_path / "snap" / "juju" / "common"
    both_extracted = Barrier(2)

    def unpack_then_wait(charm_file: Path, destination: Path) -> Path:
        unpacked = unpack_charm_artifact(charm_file, destination)
        both_extracted.wait(timeout=5)
        return unpacked

    monkeypatch.setattr("test_suite.conftest.unpack_charm_artifact", unpack_then_wait)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: conftest._unpack_local_charm_artifact(artifact, project_root, snap_common),
                range(2),
            )
        )

    assert results[0] == results[1]
    assert results[0].joinpath("metadata.yaml").read_text(encoding="utf-8") == "name: test-charm\n"


def test_generated_bundle_directory_is_stable_per_model_and_isolated_per_mount(tmp_path: Path) -> None:
    snap_common = tmp_path / "snap" / "juju" / "common"
    model = JujuModelHandle(controller="controller", model="model")

    first_mount = conftest._generated_bundle_directory(tmp_path / "mount-a" / "project", model, snap_common)
    resumed_run = conftest._generated_bundle_directory(tmp_path / "mount-a" / "project", model, snap_common)
    second_mount = conftest._generated_bundle_directory(tmp_path / "mount-b" / "project", model, snap_common)
    other_model = conftest._generated_bundle_directory(
        tmp_path / "mount-a" / "project",
        JujuModelHandle(controller="controller", model="another-model"),
        snap_common,
    )

    assert first_mount == resumed_run
    assert first_mount != second_mount
    assert first_mount != other_model


def test_generated_bundle_directory_without_snap_uses_project_cache(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    output = conftest._generated_bundle_directory(
        project_root,
        JujuModelHandle(controller="controller", model="model"),
        juju_snap_common=None,
    )

    assert output.is_relative_to(project_root / ".pytest_cache" / "charm-integration-testing" / "generated-bundles")
