# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from extensions import SupersetSigningSecretExtension
from juju import JujuModelHandle
from juju.models import JujuApplicationInfo

from .shared import NullJujuBackend

MODEL = JujuModelHandle(controller="controller", model="model")


@dataclass
class SupersetBackend(NullJujuBackend):
    applications: dict[str, str] = field(default_factory=lambda: {"neighbor": "superset-k8s"})
    configs: dict[str, dict[str, Any]] = field(default_factory=lambda: {"neighbor": {"signing-keys-secret-id": None}})
    secrets: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    grants: list[tuple[str, str]] = field(default_factory=list)
    fail_grant: bool = False

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return {name: JujuApplicationInfo(charm=charm, revision=0) for name, charm in self.applications.items()}

    def get_application_config(self, model: JujuModelHandle, application: str) -> dict[str, Any]:
        return self.configs[application].copy()

    def add_secret(self, model: JujuModelHandle, name: str, values: dict[str, str]) -> str:
        self.secrets.append((name, values))
        return "secret:abc"

    def grant_secret(self, model: JujuModelHandle, name_or_id: str, application: str) -> None:
        if self.fail_grant:
            raise RuntimeError("Grant failed")
        self.grants.append((name_or_id, application))

    def configure_application(self, model: JujuModelHandle, application: str, values: dict[str, str]) -> None:
        self.configs[application].update(values)


def test_missing_secret_is_created_granted_and_preserved_on_redeploy() -> None:
    backend = SupersetBackend()
    extension = SupersetSigningSecretExtension(backend, logging.getLogger(__name__))

    extension.post_deploy(MODEL)
    extension.post_deploy(MODEL)

    assert len(backend.secrets) == 1
    content = backend.secrets[0][1]
    assert set(content) == {"secret-key", "async-queries-jwt"}
    assert all(len(value) >= 32 for value in content.values())
    assert content["secret-key"] != content["async-queries-jwt"]
    assert backend.grants == [("secret:abc", "neighbor")]
    assert backend.configs["neighbor"]["signing-keys-secret-id"] == "secret:abc"


@pytest.mark.parametrize("config", [{}, {"signing-keys-secret-id": "secret:existing"}])
def test_legacy_or_existing_config_is_not_overwritten(config: dict[str, str]) -> None:
    backend = SupersetBackend(configs={"neighbor": config})

    SupersetSigningSecretExtension(backend, logging.getLogger(__name__)).post_deploy(MODEL)

    assert backend.secrets == []
    assert backend.configs["neighbor"] == config


def test_grant_error_is_propagated_without_configuring() -> None:
    backend = SupersetBackend(fail_grant=True)

    with pytest.raises(RuntimeError, match="Grant failed"):
        SupersetSigningSecretExtension(backend, logging.getLogger(__name__)).post_deploy(MODEL)

    assert backend.configs["neighbor"]["signing-keys-secret-id"] is None


def test_multiple_superset_applications_require_explicit_secret_scope() -> None:
    backend = SupersetBackend(
        applications={"ui": "superset-k8s", "worker": "superset-k8s"},
        configs={"ui": {"signing-keys-secret-id": ""}, "worker": {"signing-keys-secret-id": ""}},
    )

    with pytest.raises(RuntimeError, match="multiple Superset applications"):
        SupersetSigningSecretExtension(backend, logging.getLogger(__name__)).post_deploy(MODEL)

    assert backend.secrets == []
