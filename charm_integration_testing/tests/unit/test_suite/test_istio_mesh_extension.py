# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import inspect
import logging
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
    def cli_version(self) -> JujuVersion:
        return JujuVersion.parse("4.0.15")

    def version(self, model: JujuModelHandle) -> JujuVersion:
        return JujuVersion.parse("3.6.29" if model.controller == "target" else "3.6.24")


@pytest.mark.parametrize("cmr,use_neighbor", [(False, False), (True, False), (True, True)])
def test_resolves_channel_for_model_architecture_and_version(cmr: bool, use_neighbor: bool) -> None:
    # GIVEN models with different architectures and Juju versions, both older than the CLI
    target = JujuModelHandle(controller="target", model="target")
    neighbor = JujuModelHandle(controller="neighbor", model="neighbor") if cmr else None
    charmhub = CharmhubStub()
    factory = cast(Callable[..., IstioMeshExtension], inspect.unwrap(istio_mesh_extension))
    juju = JujuStub(applications={"beacon": "istio-beacon-k8s"})
    extension = factory(
        cast(CharmhubClient, charmhub), juju, logging.getLogger("test"), target, "amd64", neighbor, "arm64"
    )
    model = neighbor if use_neighbor else target
    assert model is not None

    # WHEN resolving the control-plane channel
    extension.post_deploy(model)

    # THEN the existing release resolver receives the model architecture and Juju version without a channel pin
    assert juju.deployed == [(model.uri, "istio-k8s", "istio-k8s")]
    assert charmhub.calls == [
        {
            "charm_name": "istio-k8s",
            "ubuntu_arch": "arm64" if use_neighbor else "amd64",
            "platform": "kubernetes",
            "juju_version": BundleJujuVersion.parse("3.6.24" if use_neighbor else "3.6.29"),
        }
    ]
