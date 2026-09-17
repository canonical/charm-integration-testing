# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from extensions.litmus import LitmusExtension
from extensions.litmus.extension import (
    CHAOSCENTER_ALIAS,
    INFRASTRUCTURE_CHARM,
    INFRASTRUCTURE_ENDPOINT,
    LitmusConfig,
    infrastructure_bundle,
    infrastructure_is_connected,
    validate_litmus_target,
    wait_for_litmus,
)
from juju import JujuApplicationInfo, JujuClient, JujuConsumedOfferInfo, JujuExtension, JujuModelHandle, JujuVersion
from juju.models import CharmChannel
from kubernetes import client as k8s_client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend, KubernetesClient

from .shared import NullJujuBackend

TARGET = JujuModelHandle(controller="target-controller", model="target-model")
NEIGHBOR = JujuModelHandle(controller="neighbor-controller", model="neighbor-model")
CONFIG = LitmusConfig("shared:admin/litmus.chaoscenter")
OFFERING_MODEL = JujuModelHandle(controller="shared", model="litmus", owner="admin")


@dataclass
class CoreApiStub:
    uid: str

    def read_namespace(self, name: str) -> k8s_client.V1Namespace:
        assert name == "kube-system"
        return k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(uid=self.uid))


@dataclass
class ClosingApiStub:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class KubernetesStub(KubernetesBackend):
    def __init__(self, uid: str = "cluster-one") -> None:
        self.core_v1_api = CoreApiStub(uid)
        self.api_client = ClosingApiStub()
        self.crds: set[str] = set()
        self.ready_namespaces: set[str] = set()
        self.reads: list[tuple[str, str]] = []
        self.error: ApiException | None = None

    def crd_exists(self, name: str) -> bool:
        if self.error is not None:
            raise self.error
        return name in self.crds

    def deployment_is_ready(self, namespace: str, name: str) -> bool:
        self.reads.append((namespace, name))
        return namespace in self.ready_namespaces


class LitmusBackendStub(NullJujuBackend):
    def __init__(self) -> None:
        self.kubernetes = KubernetesStub()
        self.clients: dict[str, KubernetesClient | None] = {
            TARGET.uri: KubernetesClient(self.kubernetes),
            NEIGHBOR.uri: KubernetesClient(self.kubernetes),
            OFFERING_MODEL.uri: KubernetesClient(KubernetesStub()),
        }
        self.resolutions: list[str] = []
        self.applications: dict[JujuModelHandle, dict[str, JujuApplicationInfo]] = {}
        self.offers: dict[JujuModelHandle, dict[str, JujuConsumedOfferInfo]] = {}
        self.connected: set[JujuModelHandle] = set()
        self.deployments: list[tuple[JujuModelHandle, str, bool, bool]] = []
        self.created_models: list[JujuModelHandle] = []
        self.deploy_error: Exception | None = None
        self.model_version = JujuVersion(3, 6, 28)
        self.establish_relation = True

    def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
        self.resolutions.append(model.uri)
        return self.clients[model.uri]

    def version(self, model: JujuModelHandle) -> JujuVersion:
        return self.model_version

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return self.applications.get(model, {})

    def list_consumed_offers(self, model: JujuModelHandle) -> dict[str, JujuConsumedOfferInfo]:
        return self.offers.get(model, {})

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: JujuModelHandle
    ) -> bool:
        assert (application_1, endpoint_1, application_2, endpoint_2) == (
            INFRASTRUCTURE_CHARM,
            INFRASTRUCTURE_ENDPOINT,
            CHAOSCENTER_ALIAS,
            INFRASTRUCTURE_ENDPOINT,
        )
        return model in self.connected

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        self.created_models.append(JujuModelHandle(controller=controller, model=model))

    def install(self, model: JujuModelHandle, offer: str = CONFIG.offer_url, channel: str = "dev/edge") -> None:
        self.applications[model] = {
            INFRASTRUCTURE_CHARM: JujuApplicationInfo(
                charm=INFRASTRUCTURE_CHARM, revision=12, channel=CharmChannel.parse(channel)
            )
        }
        self.offers[model] = {CHAOSCENTER_ALIAS: JujuConsumedOfferInfo(offer, frozenset({INFRASTRUCTURE_ENDPOINT}))}
        if self.establish_relation:
            self.connected.add(model)
        self.kubernetes.crds.add("chaosengines.litmuschaos.io")
        self.kubernetes.ready_namespaces.add(model.model)

    def deploy_bundle_file(
        self,
        model: JujuModelHandle,
        bundle: str,
        timeout: timedelta | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        content = Path(bundle).read_text()
        self.deployments.append((model, content, trust, force))
        if self.deploy_error is not None:
            raise self.deploy_error
        data = yaml.safe_load(content)
        if INFRASTRUCTURE_CHARM in data.get("applications", {}):
            self.install(
                model,
                offer=data["saas"][CHAOSCENTER_ALIAS]["url"],
                channel=data["applications"][INFRASTRUCTURE_CHARM]["channel"],
            )


def make_client(backend: LitmusBackendStub, configs: dict[JujuModelHandle, LitmusConfig]) -> JujuClient:
    return JujuClient(backend, logging.getLogger(__name__), extensions=[LitmusExtension(backend, configs)])


class TestLitmusExtension:
    def test_prepares_new_model_through_client(self) -> None:
        # GIVEN a configured model on the same cluster as ChaosCenter
        backend = LitmusBackendStub()
        client = make_client(backend, {TARGET: CONFIG})

        # WHEN JujuClient creates the model
        client.add_model(TARGET.controller, TARGET.model, {})

        # THEN the hook deploys only infrastructure and consumes the existing offer
        assert backend.created_models == [TARGET]
        assert len(backend.deployments) == 1
        model, content, trust, force = backend.deployments[0]
        bundle = yaml.safe_load(content)
        assert model == TARGET
        assert bundle == {
            "bundle": "kubernetes",
            "applications": {
                INFRASTRUCTURE_CHARM: {"charm": INFRASTRUCTURE_CHARM, "channel": "dev/edge", "num_units": 1}
            },
            "saas": {CHAOSCENTER_ALIAS: {"url": CONFIG.offer_url}},
            "relations": [
                [f"{INFRASTRUCTURE_CHARM}:{INFRASTRUCTURE_ENDPOINT}", f"{CHAOSCENTER_ALIAS}:{INFRASTRUCTURE_ENDPOINT}"]
            ],
        }
        assert trust is False
        assert force is False
        assert backend.kubernetes.reads == [(TARGET.model, "chaos-operator-ce")]

    def test_post_deploy_is_not_reentrant(self, tmp_path: Path) -> None:
        # GIVEN a client with a second hook recording deployment notifications
        class HookSpy(JujuExtension):
            def __init__(self) -> None:
                self.models: list[JujuModelHandle] = []

            def post_deploy(self, model: JujuModelHandle) -> None:
                self.models.append(model)

        backend = LitmusBackendStub()
        client = make_client(backend, {TARGET: CONFIG})
        spy = HookSpy()
        client.extensions.append(spy)
        path = tmp_path / "target.yaml"
        path.write_text("applications: {}\n")

        # WHEN deploying and redeploying the target bundle
        client.deploy_bundle_file(str(path), TARGET)
        client.deploy_bundle_file(str(path), TARGET)

        # THEN infrastructure is deployed once and hooks run only for the caller's deployments
        assert len(backend.deployments) == 3
        assert spy.models == [TARGET, TARGET]
        assert len(backend.kubernetes.reads) == 2

    def test_unconfigured_model_is_untouched(self) -> None:
        # GIVEN no Litmus configuration
        backend = LitmusBackendStub()
        client = make_client(backend, {})

        # WHEN creating a model
        client.add_model(TARGET.controller, TARGET.model, {})

        # THEN no Kubernetes or deployment operation is added
        assert backend.resolutions == []
        assert backend.deployments == []

    def test_models_have_independent_configuration(self) -> None:
        # GIVEN separate offers for target and neighbor
        backend = LitmusBackendStub()
        neighbor_config = LitmusConfig("shared:admin/litmus.neighbor-offer")
        client = make_client(backend, {TARGET: CONFIG, NEIGHBOR: neighbor_config})

        # WHEN both models are created
        client.add_model(NEIGHBOR.controller, NEIGHBOR.model, {})
        client.add_model(TARGET.controller, TARGET.model, {})

        # THEN neither model inherits the other's offer
        assert backend.offers[TARGET][CHAOSCENTER_ALIAS].url == CONFIG.offer_url
        assert backend.offers[NEIGHBOR][CHAOSCENTER_ALIAS].url == neighbor_config.offer_url

    def test_unconfigured_neighbor_does_not_inherit_offer(self) -> None:
        # GIVEN an offer configured only for the target
        backend = LitmusBackendStub()
        client = make_client(backend, {TARGET: CONFIG})

        # WHEN creating the neighbor model
        client.add_model(NEIGHBOR.controller, NEIGHBOR.model, {})

        # THEN no infrastructure is deployed there
        assert backend.deployments == []

    def test_deployment_error_propagates(self) -> None:
        # GIVEN a failed bundle deployment
        backend = LitmusBackendStub()
        error = RuntimeError("Deployment failed")
        backend.deploy_error = error
        client = make_client(backend, {TARGET: CONFIG})

        # WHEN creating a configured model, THEN the original error propagates
        with pytest.raises(RuntimeError) as exc_info:
            client.add_model(TARGET.controller, TARGET.model, {})
        assert exc_info.value is error

    def test_missing_relation_fails_without_fallback(self) -> None:
        # GIVEN a deployment that does not establish its relation
        backend = LitmusBackendStub()
        backend.establish_relation = False
        client = make_client(backend, {TARGET: CONFIG})

        # WHEN provisioning, THEN readiness alone cannot make the setup succeed
        with pytest.raises(RuntimeError, match="relation was not established"):
            client.add_model(TARGET.controller, TARGET.model, {})


class TestValidateTarget:
    def test_unsupported_backend_fails_explicitly(self) -> None:
        # GIVEN a backend without model-aware resolution
        backend = NullJujuBackend()

        # WHEN resolving a model, THEN it does not guess from the controller
        with pytest.raises(NotImplementedError, match="Model-aware"):
            backend.get_kubernetes_client_for_model(TARGET)

    def test_rejects_different_cluster(self) -> None:
        # GIVEN a remote ChaosCenter on another cluster
        backend = LitmusBackendStub()
        backend.clients[OFFERING_MODEL.uri] = KubernetesClient(KubernetesStub(uid="other-cluster"))

        # WHEN validating the target, THEN deployment is rejected
        with pytest.raises(ValueError, match="same Kubernetes cluster"):
            validate_litmus_target(backend, TARGET, CONFIG)
        assert backend.deployments == []

    def test_rejects_machine_target(self) -> None:
        # GIVEN an offer configured for a machine model
        backend = LitmusBackendStub()
        backend.clients[TARGET.uri] = None

        # WHEN validating, THEN the configuration error is not skipped
        with pytest.raises(ValueError, match="non-Kubernetes"):
            validate_litmus_target(backend, TARGET, CONFIG)

    def test_rejects_old_juju(self) -> None:
        # GIVEN a model below the charm's Juju requirement
        backend = LitmusBackendStub()
        backend.model_version = JujuVersion(3, 5, 7)

        # WHEN validating, THEN the requirement is reported
        with pytest.raises(ValueError, match="3.6 or newer"):
            validate_litmus_target(backend, TARGET, CONFIG)

    def test_resolves_clients_again(self) -> None:
        # GIVEN a previously validated target
        backend = LitmusBackendStub()
        validate_litmus_target(backend, TARGET, CONFIG)
        backend.clients[TARGET.uri] = KubernetesClient(KubernetesStub(uid="changed-cluster"))

        # WHEN the model resolves to a different cluster, THEN it is not hidden by a cache
        with pytest.raises(ValueError, match="same Kubernetes cluster"):
            validate_litmus_target(backend, TARGET, CONFIG)

    def test_resolves_workload_and_owned_offering_model(self) -> None:
        # GIVEN model clients without a controller-model resolver
        backend = LitmusBackendStub()

        # WHEN validating the target
        kubernetes = validate_litmus_target(backend, TARGET, CONFIG)

        # THEN both actual models are queried, including the offer owner
        assert kubernetes is backend.kubernetes
        assert backend.resolutions == [TARGET.uri, OFFERING_MODEL.uri]

    def test_rejects_different_clusters_on_one_controller(self) -> None:
        # GIVEN two models on one controller but on different clusters
        backend = LitmusBackendStub()
        offer = JujuModelHandle(controller=TARGET.controller, model="litmus", owner="admin")
        backend.clients[offer.uri] = KubernetesClient(KubernetesStub(uid="other-cluster"))
        config = LitmusConfig(f"{offer.uri}.chaoscenter")

        # WHEN validating, THEN the shared controller does not imply a shared cluster
        with pytest.raises(ValueError, match="same Kubernetes cluster"):
            validate_litmus_target(backend, TARGET, config)
        assert backend.resolutions == [TARGET.uri, offer.uri]


class TestExistingInfrastructure:
    @dataclass(frozen=True)
    class Params:
        label: str
        application: str = INFRASTRUCTURE_CHARM
        charm: str = INFRASTRUCTURE_CHARM
        channel: str = "dev/edge"
        offer: str = CONFIG.offer_url
        endpoints: frozenset[str] = frozenset({INFRASTRUCTURE_ENDPOINT})

    test_cases = [
        Params(label="charm", charm="other-charm"),
        Params(label="channel", channel="latest/stable"),
        Params(label="offer", offer="shared:admin/litmus.other-offer"),
        Params(label="endpoint", endpoints=frozenset()),
        Params(label="alias", application="other-infrastructure"),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.label)
    def test_rejects_conflicts_without_modification(self, params: Params) -> None:
        # GIVEN conflicting existing resources
        backend = LitmusBackendStub()
        backend.install(TARGET)
        backend.applications[TARGET] = {
            params.application: JujuApplicationInfo(
                charm=params.charm, revision=12, channel=CharmChannel.parse(params.channel)
            )
        }
        backend.offers[TARGET] = {CHAOSCENTER_ALIAS: JujuConsumedOfferInfo(params.offer, params.endpoints)}

        # WHEN checking connectivity
        with pytest.raises(ValueError):
            infrastructure_is_connected(backend, TARGET, CONFIG)

        # THEN conflicting resources are not redeployed
        assert backend.deployments == []

    def test_detects_removed_relation(self) -> None:
        # GIVEN an infrastructure connection observed earlier
        backend = LitmusBackendStub()
        backend.install(TARGET)
        assert infrastructure_is_connected(backend, TARGET, CONFIG)

        # WHEN the relation is removed, THEN the next check observes the change
        backend.connected.remove(TARGET)
        assert not infrastructure_is_connected(backend, TARGET, CONFIG)


class TestPartialInfrastructure:
    @dataclass(frozen=True)
    class Params:
        label: str
        application: bool
        offer: bool

    test_cases = [
        Params(label="application-only", application=True, offer=False),
        Params(label="offer-only", application=False, offer=True),
        Params(label="missing-relation", application=True, offer=True),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.label)
    def test_requests_bundle_for_partial_connection(self, params: Params, tmp_path: Path) -> None:
        # GIVEN matching but incomplete infrastructure in an existing model
        backend = LitmusBackendStub()
        backend.install(TARGET)
        backend.connected.clear()
        if not params.application:
            backend.applications[TARGET].clear()
        if not params.offer:
            backend.offers[TARGET].clear()
        client = make_client(backend, {TARGET: CONFIG})
        path = tmp_path / "workload.yaml"
        path.write_text("applications: {}\n")

        # WHEN a workload deployment triggers the extension through JujuClient
        client.deploy_bundle_file(str(path), TARGET)

        # THEN it requests infrastructure completion without force or trust escalation
        assert len(backend.deployments) == 2
        model, content, trust, force = backend.deployments[1]
        assert model == TARGET
        assert yaml.safe_load(content) == yaml.safe_load(infrastructure_bundle(CONFIG))
        assert trust is False
        assert force is False
        assert TARGET in backend.connected

        # WHEN the stub reports a completed connection, THEN no further infrastructure deploy is requested
        client.deploy_bundle_file(str(path), TARGET)
        assert len(backend.deployments) == 3


class TestWaitForLitmus:
    def test_waits_for_operator_after_crd(self) -> None:
        # GIVEN the CRD without a ready operator
        backend = KubernetesStub()
        backend.crds.add("chaosengines.litmuschaos.io")
        sleeps: list[float] = []

        def become_ready(seconds: float) -> None:
            sleeps.append(seconds)
            backend.ready_namespaces.add(TARGET.model)

        # WHEN the operator becomes ready on the next poll
        wait_for_litmus(backend, TARGET.model, timedelta(seconds=10), sleep=become_ready)

        # THEN the existing CRD was not mistaken for readiness
        assert len(sleeps) == 1
        assert len(backend.reads) == 2

    def test_timeout_fails(self) -> None:
        # GIVEN no Litmus resources and an exhausted wait budget
        backend = KubernetesStub()

        # WHEN waiting, THEN the timeout is an error rather than unavailability
        with pytest.raises(TimeoutError, match="did not become ready"):
            wait_for_litmus(backend, TARGET.model, timedelta())

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_api_error_propagates_without_retry(self, status: int) -> None:
        # GIVEN an API error
        backend = KubernetesStub()
        backend.error = ApiException(status=status)
        sleeps: list[float] = []

        # WHEN checking readiness, THEN the API error is not retried or converted to a timeout
        with pytest.raises(ApiException) as exc_info:
            wait_for_litmus(backend, TARGET.model, timedelta(seconds=10), sleep=sleeps.append)
        assert exc_info.value is backend.error
        assert sleeps == []


class TestLitmusConfig:
    @pytest.mark.parametrize("url", ["", "admin/litmus.offer", "shared:admin/litmus.offer "])
    def test_rejects_invalid_offer(self, url: str) -> None:
        # GIVEN an incomplete or whitespace-containing URL
        # WHEN creating configuration, THEN it is rejected before any deployment
        with pytest.raises(ValueError, match="offer"):
            LitmusConfig(url)

    def test_rejects_nonpositive_timeout(self) -> None:
        # GIVEN no positive wait budget
        # WHEN creating configuration, THEN it is rejected
        with pytest.raises(ValueError, match="positive"):
            LitmusConfig(CONFIG.offer_url, timeout=timedelta())
