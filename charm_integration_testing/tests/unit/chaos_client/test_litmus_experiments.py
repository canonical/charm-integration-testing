# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

import pytest
from chaos_client.litmus_experiments import (
    LitmusExperiment,
    experiment_manifest,
    role_binding_manifest,
    role_manifest,
    service_account_manifest,
)


class TestExperimentManifest:
    @dataclass(frozen=True)
    class Params:
        experiment: LitmusExperiment
        settings: dict[str, str]

    @pytest.mark.parametrize(
        "params",
        [
            Params("pod-cpu-hog", {"CPU_CORES": "1", "CPU_LOAD": "100"}),
            Params("pod-memory-hog", {"MEMORY_CONSUMPTION": "500", "NUMBER_OF_WORKERS": "1"}),
        ],
        ids=lambda params: params.experiment,
    )
    def test_unique_resource_name_preserves_experiment_command(self, params: Params) -> None:
        # GIVEN an execution name distinct from the upstream experiment name
        name = "cit-execution"

        # WHEN building the experiment definition
        manifest = experiment_manifest("model", name, params.experiment)

        # THEN the definition uses pinned images and the original experiment entry point
        assert manifest["metadata"] == {"name": name, "namespace": "model"}
        spec = manifest["spec"]
        assert isinstance(spec, dict)
        definition = spec["definition"]
        assert definition["args"] == ["-c", f"./experiments -name {params.experiment}"]
        assert definition["image"] == "litmuschaos.docker.scarf.sh/litmuschaos/go-runner:3.31.0"
        env = {entry["name"]: entry["value"] for entry in definition["env"]}
        assert env["LIB_IMAGE"] == definition["image"]
        assert params.settings.items() <= env.items()
        assert env["CONTAINER_RUNTIME"] == "containerd"
        assert env["SOCKET_PATH"] == "/run/containerd/containerd.sock"
        assert definition["labels"]["name"] == name


def test_role_matches_experiment_permissions_and_binding_targets_account() -> None:
    # GIVEN definitions for one execution in a model namespace
    experiment = experiment_manifest("model", "cit-test", "pod-cpu-hog")
    account = service_account_manifest("model", "cit-test")
    role = role_manifest("model", "cit-test")

    # WHEN binding the runner permissions
    binding = role_binding_manifest("model", "cit-test")

    # THEN the namespace role matches the declared permissions and the binding is local
    spec = experiment["spec"]
    assert isinstance(spec, dict)
    assert role["rules"] == spec["definition"]["permissions"]
    assert role["kind"] == "Role"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "cit-test", "namespace": "model"}]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "cit-test"}
    assert account["metadata"] == role["metadata"] == binding["metadata"]


def test_manifests_do_not_share_mutable_permissions() -> None:
    # GIVEN a manifest whose permissions are changed by its caller
    first = role_manifest("model", "first")
    rules = first["rules"]
    assert isinstance(rules, list)
    rules.clear()

    # WHEN building another execution's role
    second = role_manifest("model", "second")

    # THEN the original permissions are intact
    assert second["rules"]
