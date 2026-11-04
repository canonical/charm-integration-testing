# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import inspect
import logging
from pathlib import Path
from typing import Any, Callable, cast

import pytest
from extensions.istio_mesh.extension import IstioMeshExtension
from juju import JujuModelHandle, JujuVersion
from test_suite.conftest import istio_mesh_extension

from bundle_builder_x import Charm, CharmChannel, CharmhubClient
from bundle_builder_x import JujuVersion as BundleJujuVersion

from ..extensions.shared import JujuStub as JujuStubBase


class CharmhubStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def charm_from_store(self, **kwargs: Any) -> Charm:
        self.calls.append(kwargs)
        return Charm(
            name="istio-k8s",
            channel=CharmChannel.model_validate("test-track/candidate"),
            revision=1,
            ubuntu_version="24.04",
            ubuntu_arch=kwargs["ubuntu_arch"],
            endpoints={},
            platforms=["kubernetes"],
        )


class JujuStub(JujuStubBase):
    def model_architecture(self, model: JujuModelHandle) -> str:
        return "amd64" if model.controller == "target" else "arm64"

    def cli_version(self) -> JujuVersion:
        return JujuVersion.parse("4.0.15")

    def version(self, model: JujuModelHandle) -> JujuVersion:
        return JujuVersion.parse("3.6.29" if model.controller == "target" else "3.6.24")


@pytest.mark.parametrize("controller", ["target", "neighbor", "migration-destination"])
def test_resolves_channel_for_model_architecture_and_version(controller: str, tmp_path: Path) -> None:
    # GIVEN models with different architectures and Juju versions, both older than the CLI
    charmhub = CharmhubStub()
    factory = cast(Callable[..., IstioMeshExtension], inspect.unwrap(istio_mesh_extension))
    juju = JujuStub()
    extension = factory(cast(CharmhubClient, charmhub), juju, logging.getLogger("test"))
    # The model can be created after the fixture, including on a migration destination.
    model = JujuModelHandle(controller=controller, model="test-model")
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("applications: {beacon: {charm: istio-beacon-k8s}}\n", encoding="utf-8")

    # WHEN resolving the control-plane channel
    extension.pre_deploy(model, bundle)

    # THEN the existing release resolver receives the model architecture and Juju version without a channel pin
    assert juju.deployed == [(model.uri, "istio-k8s", "istio-k8s")]
    assert charmhub.calls == [
        {
            "charm_name": "istio-k8s",
            "ubuntu_arch": "amd64" if controller == "target" else "arm64",
            "platform": "kubernetes",
            "juju_version": BundleJujuVersion.parse("3.6.29" if controller == "target" else "3.6.24"),
        }
    ]
