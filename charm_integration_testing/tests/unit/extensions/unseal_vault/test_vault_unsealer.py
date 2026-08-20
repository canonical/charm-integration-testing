# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from io import StringIO
from typing import Any, NamedTuple
from unittest import mock

import pytest
import yaml
from extensions.unseal_vault.vault_client import (
    VaultClient,
    VaultClientJujuExec,
    VaultClientJujuExecPebble,
    VaultStatus,
    VaultTokenSecret,
)
from extensions.unseal_vault.vault_unsealer import CharmInfo, VaultUnsealer, order_apps_by_dependency
from juju import JujuExecOutput, JujuModelHandle, JujuWaitTimeoutError
from juju.backend import JujuTask
from juju.models import JujuApplicationInfo, JujuIntegration, JujuIntegrationApplication

from ..shared import NullJujuBackend

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")


@dataclass
class JujuStub(NullJujuBackend):
    apps: list[str] = field(default_factory=list)
    charm_name: str = ""
    scaled_apps: list[str] = field(default_factory=list)
    settled_apps: list[str] = field(default_factory=list)
    units: dict[str, list[str]] = field(default_factory=dict)
    messages: list[tuple[str, str, timedelta | None]] = field(default_factory=list)
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    secrets_granted: list[tuple[str, str]] = field(default_factory=list)
    actions_run: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    integrations: set[JujuIntegration] = field(default_factory=set)
    exec_unit_calls: list[tuple[JujuModelHandle, str, str]] = field(default_factory=list)
    exec_units_output: list[JujuExecOutput] = field(default_factory=list)
    # Optional per-unit "current" workload message. When set for a unit, wait_for_unit_message
    # raises JujuWaitTimeoutError unless the requested message matches, simulating a real charm
    # that has moved past (or not yet reached) that message. Units not present here always
    # succeed immediately, preserving existing tests' assumptions.
    unit_messages: dict[str, str] = field(default_factory=dict)

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        return {app: JujuApplicationInfo(charm=self.charm_name, revision=0) for app in self.apps}

    def application_charm(self, model: JujuModelHandle, application: str) -> str:
        return self.charm_name

    def wait_application_scaled(self, model: JujuModelHandle, app: str, timeout: timedelta | None) -> None:
        self.scaled_apps.append(app)

    def wait_application_settled(self, model: JujuModelHandle, app: str, timeout: timedelta | None) -> None:
        self.settled_apps.append(app)

    def application_units(self, model: JujuModelHandle, app: str) -> list[str]:
        return self.units.get(app, [])

    def num_units(self, model: JujuModelHandle, app: str) -> int:
        return len(self.units.get(app, []))

    def wait_for_unit_message(self, model: JujuModelHandle, unit: str, message: str, timeout: timedelta | None) -> None:
        self.messages.append((unit, message, timeout))
        current = self.unit_messages.get(unit)
        if current is not None and message.lower() not in current.lower():
            raise JujuWaitTimeoutError()

    def add_secret(self, model: JujuModelHandle, name: str, content: dict[str, str]) -> str:
        self.secrets[name] = content
        return "secret-id"

    def grant_secret(self, model: JujuModelHandle, name: str, app: str) -> None:
        self.secrets_granted.append((name, app))

    def run_action(self, model: JujuModelHandle, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        self.actions_run.append((unit, action, params))
        return JujuTask("", 0, "", "", "")  # Dummy; provided to satisfy return type

    def remove_secret(self, model: JujuModelHandle, name: str) -> None:
        try:
            del self.secrets[name]
        except KeyError as err:
            raise subprocess.CalledProcessError(-1, ["remove", "unknown"], stderr="did not find it") from err

    def read_secret(self, model: JujuModelHandle, name: str) -> dict[str, str]:
        return self.secrets[name]

    def list_integrations(self, model: JujuModelHandle) -> set[JujuIntegration]:
        _ = model
        return self.integrations

    def set_integrations(self, integrations: set[tuple[str, str]]) -> None:
        for provider, requirer in integrations:
            self.integrations.add(
                JujuIntegration(
                    provider=JujuIntegrationApplication(
                        application=provider,
                        endpoint=f"{provider}_endpoint",
                    ),
                    requirer=JujuIntegrationApplication(
                        application=requirer,
                        endpoint=f"endpoint_{requirer}",
                    ),
                    interface=f"{provider}-to-{requirer}",
                )
            )

    def exec_unit(self, model: JujuModelHandle, unit: str, task: str, operator: bool = False) -> JujuExecOutput:
        self.exec_unit_calls.append((model, unit, task))
        return self.exec_units_output.pop(0)


@dataclass
class VaultStub(VaultClient):
    initialized_units: dict[str, bool] = field(default_factory=dict)
    sealed_units: dict[str, bool] = field(default_factory=dict)
    inits: list[str] = field(default_factory=list)
    inits_auto_unsealed: list[str] = field(default_factory=list)
    unseals: list[str] = field(default_factory=list)
    tokens: VaultTokenSecret = field(default_factory=lambda: VaultTokenSecret(root_token="root", unseal_key="key"))

    def status(self, model: JujuModelHandle, unit: str) -> VaultStatus:
        return VaultStatus(
            initialized=self.initialized_units.get(unit, False),
            sealed=self.sealed_units.get(unit, True),
            type="shamir",
        )

    def init(self, model: JujuModelHandle, unit: str, will_auto_unseal: bool = False) -> VaultTokenSecret:
        if will_auto_unseal:
            self.inits_auto_unsealed.append(unit)
        else:
            self.inits.append(unit)
        return self.tokens

    def unseal(self, model: JujuModelHandle, unit: str, tokens: VaultTokenSecret) -> None:
        self.unseals.append(unit)


class LoggerStub(logging.Logger):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:  # type: ignore[override]
        self.messages.append(message)


@mock.patch("time.sleep", new=lambda _: None)
class TestVaultUnsealer:
    def test_try_init_or_unseal_all_vaults(self) -> None:
        # GIVEN
        juju = JujuStub(apps=["vault"], charm_name="vault", units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_all_vaults(TEST_MODEL)

        # THEN
        assert "vault" in juju.scaled_apps
        assert "vault/leader" in vault.inits

    def test_try_init_or_unseal_all_vaults_will_order_provider_first(self) -> None:
        # GIVEN
        juju = JujuStub(
            apps=["vault1", "vault2"],
            charm_name="vault",
            units={"vault1": ["vault1/leader"], "vault2": ["vault2/leader"]},
        )
        juju.set_integrations({("vault2", "vault1")})  # using convenience method

        vault = VaultStub(initialized_units={"vault1/leader": False, "vault2/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_all_vaults(TEST_MODEL)

        # THEN
        assert "vault1" in juju.scaled_apps
        assert "vault2" in juju.scaled_apps
        assert "vault1/leader" in vault.inits
        assert "vault2/leader" in vault.inits
        assert vault.inits.index("vault2/leader") < vault.inits.index("vault1/leader")

    def test_try_init_vault_skips_init_and_unseal_if_already_initialized(self) -> None:
        # GIVEN
        juju = JujuStub(
            units={"vault": ["vault/leader"]},
            secrets={"vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"}},
        )
        vault = VaultStub(initialized_units={"vault/leader": True})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")

        # THEN
        assert vault.inits == []
        assert vault.unseals == []

    def test_try_init_vault_still_authorizes_if_already_initialized_but_not_authorized(self) -> None:
        # Regression test for issue #797: deploy_bundles() invokes post_deploy() (and thus
        # try_init_vault) once per deploy phase. If a prior call already initialized vault but
        # authorization hasn't happened yet, this call must still complete it instead of
        # silently no-oping.
        # GIVEN
        juju = JujuStub(
            units={"vault": ["vault/leader"]},
            secrets={"vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"}},
        )
        vault = VaultStub(initialized_units={"vault/leader": True})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")

        # THEN
        assert vault.inits == []
        assert vault.unseals == []
        assert ("vault/leader", "authorize-charm", {"secret-id": "secret-id"}) in juju.actions_run

    def test_try_init_vault_does_not_re_authorize_if_already_authorized(self) -> None:
        # GIVEN
        juju = JujuStub(
            units={"vault": ["vault/leader"]},
            secrets={"vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"}},
            # Simulate the charm having moved past the authorize message already (e.g. it's
            # active now), so the cheap peek check should report "not awaiting authorization".
            unit_messages={"vault/leader": "active"},
        )
        vault = VaultStub(initialized_units={"vault/leader": True})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")

        # THEN
        assert vault.inits == []
        assert vault.unseals == []
        for target, action, _ in juju.actions_run:
            assert (target, action) != ("vault/leader", "authorize-charm")

    def test_try_unseal_vault_unseals_if_initialized_and_sealed(self) -> None:
        # GIVEN
        juju = JujuStub(
            units={"vault": ["vault/0", "vault/1"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(
            initialized_units={"vault/0": True, "vault/1": True}, sealed_units={"vault/0": True, "vault/1": False}
        )
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN
        assert "vault/0" in vault.unseals
        assert "vault/1" not in vault.unseals

    def test_try_unseal_vault_waits_for_unit_to_finish_joining_raft_cluster(self) -> None:
        # GIVEN a non-leader unit that only reports as initialized after a few status checks,
        # simulating the delay of it auto-joining the raft cluster after being scaled up
        juju = JujuStub(
            units={"vault": ["vault/0", "vault/1"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(sealed_units={"vault/0": True, "vault/1": True})
        checks_before_initialized = [False, False, True]

        def status(_: JujuModelHandle, unit: str) -> VaultStatus:
            if unit == "vault/1":
                return VaultStatus(initialized=checks_before_initialized.pop(0), sealed=True, type="shamir")
            return VaultStatus(initialized=True, sealed=True, type="shamir")

        vault.status = status  # type: ignore
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN both units end up unsealed, once vault/1 finally reports as initialized
        assert "vault/0" in vault.unseals
        assert "vault/1" in vault.unseals
        assert checks_before_initialized == []

    def test_try_unseal_vault_skips_unit_that_never_finishes_joining_raft_cluster(self) -> None:
        # GIVEN a non-leader unit that never reports as initialized
        juju = JujuStub(
            units={"vault": ["vault/0", "vault/1"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(
            initialized_units={"vault/0": True, "vault/1": False},
            sealed_units={"vault/0": True, "vault/1": True},
        )
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN the never-initialized unit is skipped, without blocking the other unit's unseal
        assert "vault/0" in vault.unseals
        assert "vault/1" not in vault.unseals

    def test_try_unseal_vault_prioritizes_already_initialized_units(self) -> None:
        # GIVEN application_units() lists a still-joining unit before an already-initialized one
        # (ordering isn't guaranteed by the Juju backend)
        juju = JujuStub(
            units={"vault": ["vault/1", "vault/0"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(
            initialized_units={"vault/0": True, "vault/1": False},
            sealed_units={"vault/0": True, "vault/1": True},
        )
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN the already-initialized unit is unsealed first, ahead of the still-joining one
        assert vault.unseals[0] == "vault/0"

    def test_try_unseal_vault_skips_manual_unseal_for_auto_unseal_type(self) -> None:
        # GIVEN a unit that finishes joining the raft cluster with a non-shamir (auto-unseal)
        # seal type, which unseals itself and doesn't need (or have) a manual unseal key
        juju = JujuStub(
            units={"vault": ["vault/0"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub(sealed_units={"vault/0": True})

        def status(_: JujuModelHandle, unit: str) -> VaultStatus:
            return VaultStatus(initialized=True, sealed=True, type="transit")

        vault.status = status  # type: ignore
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN no manual unseal is attempted, since the unit will auto-unseal itself
        assert vault.unseals == []

    def test_try_unseal_vault_rechecks_sealed_status_after_waiting_for_initialization(self) -> None:
        # GIVEN a unit that auto-unseals itself while it's still being waited on to finish
        # joining the raft cluster
        juju = JujuStub(
            units={"vault": ["vault/0"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub()
        checks = [
            VaultStatus(initialized=False, sealed=True, type="transit"),
            VaultStatus(initialized=True, sealed=False, type="transit"),
        ]

        def status(_: JujuModelHandle, unit: str) -> VaultStatus:
            return checks.pop(0)

        vault.status = status  # type: ignore
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN no manual unseal is attempted, since the unit is no longer sealed
        assert vault.unseals == []
        assert checks == []

    def test_try_unseal_vault_skips_unit_when_status_raises(self) -> None:
        # GIVEN two units where status() raises for the first but succeeds for the second
        juju = JujuStub(
            units={"vault": ["vault/0", "vault/1"]},
            secrets={
                "vault-secret-application-vault-tokens": {"root-token": "abc", "unseal-key": "xyz"},
            },
        )
        vault = VaultStub()

        def status(_: JujuModelHandle, unit: str) -> VaultStatus:
            if unit == "vault/0":
                raise RuntimeError("connection refused")
            return VaultStatus(initialized=True, sealed=True, type="shamir")

        vault.status = status  # type: ignore
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_unseal_vault(TEST_MODEL, "vault")

        # THEN the failing unit is skipped and the healthy unit is still unsealed
        assert vault.unseals == ["vault/1"]

    def test_try_init_vault_authorizes_charm_by_default(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")

        # THEN
        assert ("vault/leader", "authorize-charm", {"secret-id": "secret-id"}) in juju.actions_run

    def test_try_init_vault_wont_authorize_charm_if_asked(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub(initialized_units={"vault/leader": False})
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault", authorize_charm=False)

        # THEN
        for target, action, _ in juju.actions_run:
            assert (target, action) != ("vault/leader", "authorize-charm")

    def test_authorize_vault_charm_runs_action_and_removes_secret(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        tokens = VaultTokenSecret(root_token="abc", unseal_key="xyz")

        # WHEN
        VaultUnsealer(charm, vault, juju, logger).authorize_vault_charm(TEST_MODEL, "vault", tokens)

        # THEN
        assert ("vault-secret-application-vault-one-time-token", "vault") in juju.secrets_granted
        assert ("vault/leader", "authorize-charm", {"secret-id": "secret-id"}) in juju.actions_run
        assert "vault-secret-application-vault-one-time-token" not in juju.secrets

    def test_save_and_get_vault_tokens(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        tokens = VaultTokenSecret(root_token="abc", unseal_key="xyz")
        unsealer = VaultUnsealer(charm, vault, juju, logger)

        # WHEN
        unsealer.save_vault_tokens(TEST_MODEL, "vault", tokens)
        result = unsealer.get_vault_tokens(TEST_MODEL, "vault")

        # THEN
        assert juju.secrets["vault-secret-application-vault-tokens"] == {"root-token": "abc", "unseal-key": "xyz"}
        assert result == tokens

    def test_saving_vault_tokens_overwrites_existing(self) -> None:
        # GIVEN
        juju = JujuStub()
        vault = VaultStub()
        logger = LoggerStub()
        charm = CharmInfo(name="vault")
        unsealer = VaultUnsealer(charm, vault, juju, logger)
        tokens_1 = VaultTokenSecret(root_token="abc", unseal_key="xyz")
        tokens_2 = VaultTokenSecret(root_token="efg", unseal_key="jkl")

        # WHEN
        unsealer.save_vault_tokens(TEST_MODEL, "vault", tokens_1)
        unsealer.save_vault_tokens(TEST_MODEL, "vault", tokens_2)
        result = unsealer.get_vault_tokens(TEST_MODEL, "vault")

        # THEN
        assert juju.secrets["vault-secret-application-vault-tokens"] == {"root-token": "efg", "unseal-key": "jkl"}
        assert result == tokens_2

    def test_vault_status_retries_on_connection_refused(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_connection_refused(model: JujuModelHandle, unit: str) -> VaultStatus:
            raise RuntimeError(
                'ERROR Failure in test_deploy: RuntimeError: Failed to query vault status: \
                         Error checking seal status: Get "https://127.0.0.1:8200/v1/sys/seal-status": dial tcp 127.0.0.1:8200: connect: connection refused'
            )

        vault.status = raise_connection_refused
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        try:
            VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")
        # THEN
        except RuntimeError as e:
            assert "connection refused" in str(e).lower()
        else:
            assert False, "Expected RuntimeError was not raised"

    def test_vault_status_does_not_retry_on_other_errors(self) -> None:
        # GIVEN
        juju = JujuStub(units={"vault": ["vault/leader"]})
        vault = VaultStub()

        def raise_other_error(model: JujuModelHandle, unit: str) -> VaultStatus:
            raise RuntimeError(
                "ERROR Failure in test_deploy: RuntimeError: Failed to query vault status: Some other error occurred"
            )

        vault.status = raise_other_error
        logger = LoggerStub()
        charm = CharmInfo(name="vault")

        # WHEN
        try:
            VaultUnsealer(charm, vault, juju, logger).try_init_vault(TEST_MODEL, "vault")
        # THEN
        except RuntimeError as e:
            assert "some other error occurred" in str(e).lower()
        else:
            assert False, "Expected RuntimeError was not raised"


class TestOrderAppsByDependency:
    class Params(NamedTuple):
        label: str
        applications: list[str]
        integrations: set[tuple[str, str]]
        expected: list[str]

    test_cases = [
        # no changes to original order
        Params(
            label="no integrations between 2",
            applications=["vault1", "vault2"],
            integrations=set(),
            expected=["vault1", "vault2"],
        ),
        Params(
            label="irrelevant integrations",
            applications=["vault1", "vault2"],
            integrations={("something1", "something2")},
            expected=["vault1", "vault2"],
        ),
        Params(
            label="no integrations preordered",
            applications=["vault2", "vault1", "vault3"],
            integrations=set(),
            expected=["vault2", "vault1", "vault3"],
        ),
        Params(
            label="irrelevant integrations preordered",
            applications=["vault2", "vault1", "vault3"],
            integrations={("something1", "something2")},
            expected=["vault2", "vault1", "vault3"],
        ),
        # flip order between 2 inter-dependents
        Params(
            label="direct dependency between 2",
            applications=["vault1", "vault2"],
            integrations={("vault2", "vault1")},
            expected=["vault2", "vault1"],
        ),
        Params(
            label="transient dependency between 2",
            applications=["vault1", "vault2"],
            integrations={("vault2", "something"), ("something", "vault1")},
            expected=["vault2", "vault1"],
        ),
        # flip order between 2 inter-dependents, moving duplicates along
        Params(
            label="direct dependency between 2 with many duplicates",
            applications=["vault1", "vault1", "vault2", "vault1"],
            integrations={("vault2", "vault1")},
            expected=["vault2", "vault1", "vault1", "vault1"],
        ),
        Params(
            label="transient dependency between 2 with 1 duplicate",
            applications=["vault1", "vault1", "vault2"],
            integrations={("vault2", "something"), ("something", "vault1")},
            expected=["vault2", "vault1", "vault1"],
        ),
        # flip order between 2 when the other has more dependencies
        Params(
            label="when 1 has dependencies but the other doesn't",
            applications=["vault1", "vault2"],
            integrations={("something", "vault1")},
            expected=["vault2", "vault1"],
        ),
        Params(
            label="when 1 has more direct dependencies than other",
            applications=["vault1", "vault2"],
            integrations={("something1", "vault2"), ("something1", "vault1"), ("something2", "vault1")},
            expected=["vault2", "vault1"],
        ),
        Params(
            label="when 1 has more transitive dependencies than other",
            applications=["vault1", "vault2"],
            integrations={("something1", "vault2"), ("something2", "vault1"), ("that", "something2")},
            expected=["vault2", "vault1"],
        ),
        # order dependent after keeping rest same order
        Params(
            label="direct dependency between first and last of 3",
            applications=["vault1", "vault2", "vault3"],
            integrations={("vault1", "vault3")},
            expected=["vault1", "vault2", "vault3"],
        ),
        Params(
            label="direct dependency between first and last of 3 preordered",
            applications=["vault3", "vault2", "vault1"],
            integrations={("vault1", "vault3")},
            expected=["vault2", "vault1", "vault3"],
        ),
        Params(
            label="direct dependency between first and last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "vault4")},
            expected=["vault3", "vault2", "vault1", "vault4"],
        ),
        Params(
            label="transient dependency between first and last of 3 preordered",
            applications=["vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "vault3")},
            expected=["vault2", "vault1", "vault3"],
        ),
        Params(
            label="2 level transient dependency between first and last of 3 preordered",
            applications=["vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "other"), ("other", "vault3")},
            expected=["vault2", "vault1", "vault3"],
        ),
        Params(
            label="transient dependency between first and last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "vault4")},
            expected=["vault3", "vault2", "vault1", "vault4"],
        ),
        Params(
            label="2 level transient dependency between first and last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "other"), ("other", "vault4")},
            expected=["vault3", "vault2", "vault1", "vault4"],
        ),
        Params(
            label="direct dependency between first and second-last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "vault3")},
            expected=["vault4", "vault2", "vault1", "vault3"],
        ),
        Params(
            label="transient dependency between first and second last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "vault3")},
            expected=["vault4", "vault2", "vault1", "vault3"],
        ),
        Params(
            label="2 level transient dependency between first and second last of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "other"), ("other", "vault3")},
            expected=["vault4", "vault2", "vault1", "vault3"],
        ),
        # order multiple dependents after original order
        Params(
            label="direct dependency between 1,3 and 1,4 of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "vault3"), ("vault1", "vault4")},
            expected=["vault2", "vault1", "vault4", "vault3"],
        ),
        Params(
            label="direct dependency between 1,3 and 3,4 of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "vault3"), ("vault3", "vault4")},
            expected=["vault2", "vault1", "vault3", "vault4"],
        ),
        Params(
            label="transient dependency between 1,3 and 4,3 of 4 preordered",
            applications=["vault4", "vault3", "vault2", "vault1"],
            integrations={("vault1", "something"), ("something", "vault3"), ("that", "something"), ("that", "vault4")},
            expected=["vault2", "vault1", "vault4", "vault3"],
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test(self, params: Params) -> None:
        result = order_apps_by_dependency(params.applications, params.integrations)
        assert result == params.expected


def dump_to_yaml_str(**kwargs: Any) -> str:
    buffer = StringIO()
    yaml.dump(kwargs, buffer)
    return buffer.getvalue()


@mock.patch("time.sleep", new=lambda _: None)
class TestAutoUnsealedVault:
    @pytest.mark.parametrize(
        "status,will_auto_unseal",
        [
            (VaultStatus(True, True, "shamir"), False),
            (VaultStatus(True, False, "shamir"), False),
            (VaultStatus(False, True, "shamir"), False),
            (VaultStatus(False, True, "transit"), True),
            (VaultStatus(False, True, "awskms"), True),
            (VaultStatus(False, True, "azurekeyvault"), True),
            (VaultStatus(False, True, "gcpckms"), True),
            (VaultStatus(False, True, "whatever"), True),  # yeah, it's a dumb match
        ],
    )
    def test_status_says_it_will_auto_unseal(self, status: VaultStatus, will_auto_unseal: bool) -> None:
        assert status.will_auto_unseal == will_auto_unseal

    @pytest.mark.parametrize("vault_impl", [VaultClientJujuExec, VaultClientJujuExecPebble])
    def test_vault_client_init_not_auto_unsealed_vault_asks_to_create_keys(
        self, vault_impl: type[VaultClientJujuExec] | type[VaultClientJujuExecPebble]
    ) -> None:
        # GIVEN exec_unit will return a usable response
        exec_output = dump_to_yaml_str(root_token="root", unseal_keys_b64=["unseal-key"])
        juju = JujuStub(exec_units_output=[JujuExecOutput(0, exec_output, "")])

        # WHEN we ask to init vault that will NOT auto-unseal
        vault_impl(juju).init(TEST_MODEL, "vault-leader", will_auto_unseal=False)

        # THEN the call was made for the unit
        call, *_ = juju.exec_unit_calls
        model, unit, task = call
        assert model == TEST_MODEL
        assert unit == "vault-leader"

        # AND vault was initialized with 1 key share and key threshold 1
        assert "vault operator init" in task
        assert "-key-shares=1" in task
        assert "-key-threshold=1" in task

    @pytest.mark.parametrize("vault_impl", [VaultClientJujuExec, VaultClientJujuExecPebble])
    def test_vault_client_init_auto_unsealed_vault_does_not_create_keys(
        self, vault_impl: type[VaultClientJujuExec] | type[VaultClientJujuExecPebble]
    ) -> None:
        # GIVEN exec_unit will return a usable response
        exec_output = dump_to_yaml_str(root_token="root", unseal_keys_b64=[])
        juju = JujuStub(exec_units_output=[JujuExecOutput(0, exec_output, "")])

        # WHEN we ask to init vault that will auto-unseal
        vault_impl(juju).init(TEST_MODEL, "vault-leader", will_auto_unseal=True)

        # THEN the call was made for the unit
        call, *_ = juju.exec_unit_calls
        model, unit, task = call
        assert model == TEST_MODEL
        assert unit == "vault-leader"

        # AND vault was initialized without asking for key-shares
        assert "vault operator init" in task
        assert "-key-shares" not in task
        assert "-key-threshold" not in task

    def test_endpoint_for_auto_unsealing(self) -> None:
        assert CharmInfo("vault").auto_unseal_requirer_endpoint == "vault-autounseal-requires"

    def test_vault_with_integrated_endpoint_should_auto_unseal(self) -> None:
        # GIVEN
        charm = CharmInfo(name="vault")
        juju = JujuStub(
            apps=["vault1", "vault2", "vault3"],
            charm_name=charm.name,
            units={"vault1": ["vault1/leader"], "vault2": ["vault2/leader"], "vault3": ["vault3/leader"]},
        )
        juju.integrations.add(
            JujuIntegration(
                provider=JujuIntegrationApplication("vault2", "irrelevant"),
                requirer=JujuIntegrationApplication("vault1", charm.auto_unseal_requirer_endpoint),
                interface="not-relevant",
            )
        )
        juju.integrations.add(
            JujuIntegration(
                provider=JujuIntegrationApplication("some-other-charm", "not-important"),
                requirer=JujuIntegrationApplication("vault3", "anything-else"),
                interface="again-not-relevant",
            )
        )

        vault = VaultStub(initialized_units={"vault1/leader": False, "vault2/leader": False, "vault3/leader": False})
        logger = LoggerStub()

        # WHEN
        vault1_should_auto_unseal = VaultUnsealer(charm, vault, juju, logger).vault_app_should_auto_unseal(
            TEST_MODEL, "vault1"
        )
        vault2_should_auto_unseal = VaultUnsealer(charm, vault, juju, logger).vault_app_should_auto_unseal(
            TEST_MODEL, "vault2"
        )
        vault3_should_auto_unseal = VaultUnsealer(charm, vault, juju, logger).vault_app_should_auto_unseal(
            TEST_MODEL, "vault3"
        )

        # THEN
        assert vault1_should_auto_unseal is True
        assert vault2_should_auto_unseal is False
        assert vault3_should_auto_unseal is False

    def test_try_init_or_unseal_vault_indicates_auto_unseal_and_does_not_unseal(self) -> None:
        # GIVEN vault should auto-unseal
        charm = CharmInfo(name="vault")
        leader = "vault/leader"
        juju = JujuStub(units={"vault": [leader]})
        juju.integrations.add(
            JujuIntegration(
                JujuIntegrationApplication("something", "irrelevant"),
                JujuIntegrationApplication("vault", charm.auto_unseal_requirer_endpoint),
                "not-relevant",
            )
        )
        logger = LoggerStub()

        # AND vault will unseal
        def vault_status_that_will_auto_unseal(_: VaultClient, unit: str) -> VaultStatus:
            assert unit == leader
            return VaultStatus(False, True, "transit")

        vault = VaultStub(initialized_units={leader: False})
        vault.status = vault_status_that_will_auto_unseal  # type: ignore

        # WHEN asked to init
        VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_vault(TEST_MODEL, "vault")

        # THEN
        assert vault.inits_auto_unsealed == [leader]
        assert vault.inits == []
        assert vault.unseals == []

    def test_try_init_or_unseal_vault_times_out_on_auto_unseal(self) -> None:
        # GIVEN vault should unseal
        charm = CharmInfo(name="vault")
        leader = "vault/leader"
        juju = JujuStub(units={"vault": [leader]})
        juju.integrations.add(
            JujuIntegration(
                JujuIntegrationApplication("something", "irrelevant"),
                JujuIntegrationApplication("vault", charm.auto_unseal_requirer_endpoint),
                "not-relevant",
            )
        )
        logger = LoggerStub()

        # AND vault will never unseal
        def vault_status_that_will_never_auto_unseal(_: VaultClient, unit: str) -> VaultStatus:
            assert unit == leader
            result = VaultStatus(False, True, "shamir")
            assert result.will_auto_unseal is False
            return result

        vault = VaultStub(initialized_units={leader: False})
        vault.status = vault_status_that_will_never_auto_unseal  # type: ignore

        # WHEN asked to init
        # THEN it will timeout
        with pytest.raises(TimeoutError):
            VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_vault(TEST_MODEL, "vault")

        # AND
        assert vault.inits_auto_unsealed == []
        assert vault.inits == []
        assert vault.unseals == []

    def test_try_init_or_unseal_vault_auto_unseal_after_a_few_tries(self) -> None:
        # GIVEN vault should unseal
        charm = CharmInfo(name="vault")
        leader = "vault/leader"
        juju = JujuStub(units={"vault": [leader]})
        juju.integrations.add(
            JujuIntegration(
                JujuIntegrationApplication("something", "irrelevant"),
                JujuIntegrationApplication("vault", charm.auto_unseal_requirer_endpoint),
                "not-relevant",
            )
        )
        logger = LoggerStub()

        denials = [True] * 10  # checking status for other things, and then not yet auto-unsealing

        # AND vault will unseal after a few tries
        def vault_status_that_will_auto_unseal_after_a_few_tries(_: VaultClient, unit: str) -> VaultStatus:
            assert unit == leader

            not_ready = VaultStatus(False, True, "shamir")
            ready = VaultStatus(False, True, "transit")
            assert not_ready.will_auto_unseal is False
            assert ready.will_auto_unseal is True

            try:
                if denials.pop():
                    return not_ready
            except IndexError:
                # ran out of denials
                return ready

            raise RuntimeError("unreachable")

        vault = VaultStub(initialized_units={leader: False})
        vault.status = vault_status_that_will_auto_unseal_after_a_few_tries  # type: ignore

        # WHEN asked to init
        # THEN it will not timeout
        VaultUnsealer(charm, vault, juju, logger).try_init_or_unseal_vault(TEST_MODEL, "vault")

        # AND
        assert vault.inits_auto_unsealed == [leader]
        assert vault.inits == []
        assert vault.unseals == []
