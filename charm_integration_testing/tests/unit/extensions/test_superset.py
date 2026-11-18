# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from extensions import SupersetSigningSecretExtension
from juju import JujuClient, JujuModelHandle
from juju.backend import JujuSecretNotFoundError
from juju.models import JujuApplicationInfo

from .shared import NullJujuBackend

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class SupersetBackend(NullJujuBackend):
    applications: dict[str, str] = field(default_factory=lambda: {"neighbor": "superset-k8s"})
    configs: dict[str, dict[str, Any]] = field(default_factory=lambda: {"neighbor": {"signing-keys-secret-id": None}})
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    grants: list[tuple[str, str]] = field(default_factory=list)
    fail_at: str | None = None

    def deploy_bundle_file(
        self,
        model: JujuModelHandle,
        bundle: str,
        timeout: timedelta | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        pass

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return {name: JujuApplicationInfo(charm=charm, revision=0) for name, charm in self.applications.items()}

    def get_application_config(self, model: JujuModelHandle, application: str) -> dict[str, Any]:
        return self.configs[application].copy()

    def add_secret(self, model: JujuModelHandle, name: str, values: dict[str, str]) -> str:
        assert name not in self.secrets
        self.secrets[name] = values
        return "abc"

    def remove_secret(self, model: JujuModelHandle, name_or_id: str) -> None:
        if self.fail_at == "remove":
            raise RuntimeError("remove failed")
        if name_or_id not in self.secrets:
            raise JujuSecretNotFoundError(1, ["remove-secret", name_or_id])
        del self.secrets[name_or_id]
        self.grants = [grant for grant in self.grants if grant[0] != name_or_id]

    def grant_secret(self, model: JujuModelHandle, name_or_id: str, application: str) -> None:
        assert name_or_id in self.secrets
        if self.fail_at == "grant":
            raise RuntimeError("grant failed")
        self.grants.append((name_or_id, application))

    def configure_application(self, model: JujuModelHandle, application: str, values: dict[str, str]) -> None:
        if self.fail_at == "configure":
            raise RuntimeError("configure failed")
        self.configs[application].update(values)
        if self.fail_at == "after configure":
            raise RuntimeError("after configure failed")


def _client(backend: SupersetBackend) -> JujuClient:
    logger = logging.getLogger(__name__)
    return JujuClient(backend, logger, [SupersetSigningSecretExtension(backend, logger)])


def test_missing_secret_is_created_granted_and_preserved_on_redeploy() -> None:
    backend = SupersetBackend()
    client = _client(backend)

    client.deploy_bundle_file("bundle.yaml", MODEL)
    client.deploy_bundle_file("bundle.yaml", MODEL)

    assert len(backend.secrets) == 1
    name, content = next(iter(backend.secrets.items()))
    assert set(content) == {"secret-key", "async-queries-jwt"}
    assert all(len(value) >= 32 for value in content.values())
    assert content["secret-key"] != content["async-queries-jwt"]
    assert backend.grants == [(name, "neighbor")]
    assert backend.configs["neighbor"]["signing-keys-secret-id"] == "secret:abc"


@pytest.mark.parametrize("config", [{}, {"signing-keys-secret-id": "secret:existing"}])
def test_legacy_or_existing_config_is_not_overwritten(config: dict[str, str]) -> None:
    backend = SupersetBackend(configs={"neighbor": config})

    _client(backend).deploy_bundle_file("bundle.yaml", MODEL)

    assert not backend.secrets
    assert backend.configs["neighbor"] == config


@pytest.mark.parametrize("fail_at", ["grant", "configure", "after configure"])
def test_retry_leaves_one_secret_and_preserves_a_committed_config(fail_at: str) -> None:
    backend = SupersetBackend(fail_at=fail_at)
    client = _client(backend)

    with pytest.raises(RuntimeError, match=f"{fail_at} failed"):
        client.deploy_bundle_file("bundle.yaml", MODEL)
    previous = backend.secrets.copy()
    backend.fail_at = None
    client.deploy_bundle_file("bundle.yaml", MODEL)

    assert backend.configs["neighbor"]["signing-keys-secret-id"] == "secret:abc"
    assert backend.secrets.keys() == previous.keys()
    assert (backend.secrets == previous) == (fail_at == "after configure")
    assert len(backend.grants) == 1


def test_removal_failure_stops_provisioning() -> None:
    backend = SupersetBackend(fail_at="remove")

    with pytest.raises(RuntimeError, match="remove failed"):
        _client(backend).deploy_bundle_file("bundle.yaml", MODEL)

    assert not backend.secrets


def test_multiple_superset_applications_require_explicit_secret_scope() -> None:
    backend = SupersetBackend(
        applications={"ui": "superset-k8s", "worker": "superset-k8s"},
        configs={"ui": {"signing-keys-secret-id": ""}, "worker": {"signing-keys-secret-id": ""}},
    )

    with pytest.raises(RuntimeError, match="multiple Superset applications"):
        _client(backend).deploy_bundle_file("bundle.yaml", MODEL)

    assert not backend.secrets
