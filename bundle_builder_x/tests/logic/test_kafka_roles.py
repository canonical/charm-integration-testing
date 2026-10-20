# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from pathlib import Path

import pytest

from bundle_builder_x import OverridesClient
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


def _make_kafka(channel: str = "4/stable") -> Charm:
    overrides = OverridesClient(overrides=Path(__file__).resolve().parents[3] / "static/charm-overrides")
    charm_channel = CharmChannel.model_validate(channel)
    return make_charm(
        "kafka",
        channel=channel,
        endpoints={
            "peer-cluster": CharmEndpoint(type=EndpointType.REQUIRES, interface="peer_cluster", optional=True),
            "peer-cluster-orchestrator": CharmEndpoint(
                type=EndpointType.PROVIDES, interface="peer_cluster", optional=True
            ),
            "client-cas": CharmEndpoint(type=EndpointType.REQUIRES, interface="certificate_transfer", optional=True),
        },
        configs=overrides.get_charm_config_overrides("kafka", charm_channel, "24.04"),
        config_defaults={"roles": "broker"},
        constraint_strs=overrides.get_charm_constraints_overrides("kafka", charm_channel, "24.04"),
    )


@dataclass(frozen=True)
class Params:
    channel: str
    with_ca: bool


@pytest.mark.parametrize(
    "params",
    [
        Params(channel=f"4/{risk}", with_ca=with_ca)
        for risk in ("stable", "candidate", "beta", "edge")
        for with_ca in (False, True)
    ],
    ids=lambda p: f"{p.channel}-{'with-ca' if p.with_ca else 'standalone'}",
)
def test_kafka_without_external_controller_enables_combined_roles(params: Params) -> None:
    # GIVEN the real Kafka override and an optional CA transfer integration
    kafka = _make_kafka(params.channel)
    ca = make_charm(
        "ca",
        endpoints={
            "send-ca-cert": CharmEndpoint(type=EndpointType.PROVIDES, interface="certificate_transfer", optional=True)
        },
    )
    builder = BundleBuilder(charmhub_client=CharmhubClientStub(kafka, ca))
    applications = {"kafka": AppSpec(charm="kafka", channel=params.channel)}
    integrations = []
    if params.with_ca:
        applications["ca"] = AppSpec(charm="ca")
        integrations.append(
            IntegrationSpec(
                application="ca", endpoint="send-ca-cert", remote_application="kafka", remote_endpoint="client-cas"
            )
        )

    # WHEN Kafka is included without a separate controller application
    bundle = build_single_model(builder, applications, integrations, platform="machine")

    # THEN Kafka runs its own controller rather than inheriting the broker-only default
    assert bundle.applications["kafka"].config["roles"] == "broker,controller"
    assert set(bundle.applications) == set(applications)
    assert len(bundle.integrations) == len(integrations)


def test_kafka_peer_cluster_assigns_broker_and_controller_roles() -> None:
    # GIVEN two Kafka applications using the documented split-controller integration
    builder = BundleBuilder(charmhub_client=CharmhubClientStub(_make_kafka()))
    applications = {name: AppSpec(charm="kafka", channel="4/stable") for name in ("broker", "controller")}
    integrations = [
        IntegrationSpec(
            application="broker",
            endpoint="peer-cluster-orchestrator",
            remote_application="controller",
            remote_endpoint="peer-cluster",
        )
    ]

    # WHEN the bundle is built
    bundle = build_single_model(builder, applications, integrations, platform="machine")

    # THEN the providing application runs brokers and the requiring application runs controllers
    broker = bundle.applications["broker"]
    assert broker.config.get("roles", broker.charm.config_defaults["roles"]) == "broker"
    assert bundle.applications["controller"].config["roles"] == "controller"
    assert set(bundle.applications) == set(applications)
    assert len(bundle.integrations) == 1
