# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from extensions.cluster_addon import CLUSTER_ADDON_MODEL_NAME, ClusterAddonExtension
from juju import CharmChannel, JujuModelHandle, JujuWaitTimeoutError
from juju.backend import JujuWaitState
from juju.models import JujuApplicationInfo

from bundle_builder_x import OverridesClient

from ..shared import JujuStub as JujuStubBase

_MODEL = JujuModelHandle(controller="ctrl", model="test-model")
_ADDON_MODEL = JujuModelHandle(controller="ctrl", model=CLUSTER_ADDON_MODEL_NAME)


@dataclass
class JujuStub(JujuStubBase):
    """Extends the shared JujuBackend stub with the primitives ClusterAddonExtension needs.

    ``applications`` is overridden here to carry a channel per application (the shared
    base's ``applications: dict[str, str]`` maps application -> charm only), and model
    existence/creation are tracked separately from application deployment so tests can
    assert on each independently.
    """

    application_infos: dict[str, JujuApplicationInfo] = field(default_factory=dict)
    deployed_addons: dict[str, JujuApplicationInfo] = field(default_factory=dict)
    existing_models: set[str] = field(default_factory=set)
    added_models: list[str] = field(default_factory=list)
    settled: list[tuple[str, str]] = field(default_factory=list)
    fail_next_add_model: bool = False
    fail_next_deploy: bool = False

    def list_applications(self, model: "JujuModelHandle") -> dict[str, JujuApplicationInfo]:
        if model.model not in self.existing_models:
            return {}
        return self.application_infos if model.model == _MODEL.model else self.deployed_addons

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        if self.fail_next_add_model:
            self.fail_next_add_model = False
            raise RuntimeError("model already exists")
        if model in self.existing_models:
            raise RuntimeError(f"model {model} already exists")
        self.added_models.append(model)
        self.existing_models.add(model)

    def wait_for_model_to_exist(self, model: "JujuModelHandle", timeout: timedelta | None) -> None:
        if model.model not in self.existing_models:
            raise JujuWaitTimeoutError(
                wait_state=JujuWaitState(message="does not exist", insufficient_status_checks=True)
            )

    def deploy_application(
        self,
        model: "JujuModelHandle",
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
        channel: str | None = None,
    ) -> None:
        if self.fail_next_deploy:
            self.fail_next_deploy = False
            raise RuntimeError("application already exists")
        self.deployed.append((model.uri, charm, application, channel))
        self.deployed_addons[charm] = JujuApplicationInfo(charm=charm, revision=0)

    def wait_application_settled(self, model: "JujuModelHandle", application: str, timeout: timedelta | None) -> None:
        self.settled.append((model.uri, application))


def _overrides_client(tmp_path: Path, yaml_content: str, filename: str = "istio-beacon-k8s.yaml") -> OverridesClient:
    (tmp_path / filename).write_text(yaml_content, encoding="utf-8")
    return OverridesClient(overrides=tmp_path, logger=logging.getLogger("test"))


class TestPostDeploy:
    def test_deploys_and_waits_for_cluster_scoped_addon(self, tmp_path: Path) -> None:
        # GIVEN an app in the model whose charm declares a cluster addon dependency
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model}
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN the addon is created in the shared cluster addon model on the same controller, not the dependent model
        assert juju.added_models == [CLUSTER_ADDON_MODEL_NAME]
        assert juju.deployed == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s", "istio-k8s", "1/stable")]
        assert juju.settled == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s")]

    def test_deploys_into_dependent_model_when_addon_scope_is_model(self, tmp_path: Path) -> None:
        # GIVEN an addon whose own override declares "model" scope
        (tmp_path / "istio-k8s.yaml").write_text("addon_scope: model\n", encoding="utf-8")
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model}
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN the addon is deployed into the dependent model itself, not a shared model
        assert juju.added_models == []
        assert juju.deployed == [(_MODEL.uri, "istio-k8s", "istio-k8s", None)]
        assert juju.settled == [(_MODEL.uri, "istio-k8s")]

    def test_ignores_apps_with_no_cluster_addons(self, tmp_path: Path) -> None:
        # GIVEN a model whose only application has no cluster_addons declared
        overrides = OverridesClient(overrides=tmp_path, logger=logging.getLogger("test"))
        juju = JujuStub()
        juju.existing_models = {_MODEL.model}
        juju.application_infos = {
            "grafana": JujuApplicationInfo(charm="grafana-k8s", revision=1, channel=CharmChannel("1", "stable", ""))
        }
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN nothing is deployed
        assert juju.deployed == []
        assert juju.added_models == []

    def test_ignores_apps_without_a_resolvable_channel(self, tmp_path: Path) -> None:
        # GIVEN an app whose channel is not yet known (e.g. reported as None by the backend)
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model}
        juju.application_infos = {"beacon": JujuApplicationInfo(charm="istio-beacon-k8s", revision=74, channel=None)}
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN nothing is deployed, since the charm/channel combination could not be resolved
        assert juju.deployed == []

    def test_skips_deploy_when_addon_already_present_in_target_model(self, tmp_path: Path) -> None:
        # GIVEN the shared cluster addon model already has istio-k8s deployed
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model, CLUSTER_ADDON_MODEL_NAME}
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        juju.deployed_addons = {"istio-k8s": JujuApplicationInfo(charm="istio-k8s", revision=59)}
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN no new model or deploy call happens, but we still wait for it to settle
        assert juju.added_models == []
        assert juju.deployed == []
        assert juju.settled == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s")]

    def test_skips_deploy_when_addon_charm_already_present_under_a_different_application_name(
        self, tmp_path: Path
    ) -> None:
        # GIVEN the shared cluster addon model already has istio-k8s deployed, but under a
        # custom application name (e.g. deployed manually, or by an unrelated bundle) rather
        # than the application name this extension would use
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model, CLUSTER_ADDON_MODEL_NAME}
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        juju.deployed_addons = {"istio-k8s-mesh": JujuApplicationInfo(charm="istio-k8s", revision=59)}
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN the existing application is recognized by charm (not app name), so no duplicate
        # deploy happens, and we wait on its actual application name rather than the charm name
        assert juju.added_models == []
        assert juju.deployed == []
        assert juju.settled == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s-mesh")]


class TestConcurrentRaceTolerance:
    def test_tolerates_a_concurrent_worker_already_creating_the_addon_model(self, tmp_path: Path) -> None:
        # GIVEN add_model fails (as if a concurrent worker created it first), but the model
        # does actually exist by the time we re-check
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model, CLUSTER_ADDON_MODEL_NAME}
        juju.fail_next_add_model = True
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN the race is tolerated and the addon is still deployed and waited on
        assert juju.deployed == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s", "istio-k8s", "1/stable")]
        assert juju.settled == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s")]

    def test_reraises_add_model_failure_if_model_still_does_not_exist(self, tmp_path: Path) -> None:
        # GIVEN add_model fails and the model still does not exist afterward (a genuine failure)
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model}
        juju.fail_next_add_model = True
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs THEN the original error propagates
        with pytest.raises(RuntimeError, match="model already exists"):
            extension.post_deploy(_MODEL)

    def test_tolerates_a_concurrent_worker_already_deploying_the_addon(self, tmp_path: Path) -> None:
        # GIVEN deploy_application fails (as if a concurrent worker deployed it first), but
        # the application does actually exist by the time we re-check
        overrides = _overrides_client(
            tmp_path,
            "overrides:\n  - cluster_addons:\n      - charm: istio-k8s\n        channel: 1/stable\n",
        )
        juju = JujuStub()
        juju.existing_models = {_MODEL.model, CLUSTER_ADDON_MODEL_NAME}
        juju.application_infos = {
            "beacon": JujuApplicationInfo(
                charm="istio-beacon-k8s", revision=74, channel=CharmChannel("1", "stable", "")
            )
        }

        class RacyJujuStub(JujuStub):
            def deploy_application(
                self,
                model: JujuModelHandle,
                charm: str,
                application: str | None = None,
                config: dict[str, Any] | None = None,
                trust: bool = False,
                force: bool = False,
                channel: str | None = None,
            ) -> None:
                # Simulate a concurrent worker winning the race: the app appears afterwards.
                self.deployed_addons = {charm: JujuApplicationInfo(charm=charm, revision=59)}
                raise RuntimeError("application already exists")

        juju = RacyJujuStub(
            existing_models={_MODEL.model, CLUSTER_ADDON_MODEL_NAME}, application_infos=juju.application_infos
        )
        extension = ClusterAddonExtension(juju, overrides, logging.getLogger("test"))

        # WHEN post_deploy runs
        extension.post_deploy(_MODEL)

        # THEN the race is tolerated and we still wait for the addon to settle
        assert juju.settled == [(f"ctrl:{CLUSTER_ADDON_MODEL_NAME}", "istio-k8s")]
