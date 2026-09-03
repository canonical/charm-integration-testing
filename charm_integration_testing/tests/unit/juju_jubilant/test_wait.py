# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import jubilant
import pytest
from juju import JujuIntegrationApplication
from juju_jubilant.wait import (
    all_statuses_are_in,
    application_is_on_revision,
    applications_are_removed,
    applications_are_scaled,
    applications_have_no_units,
    bundle_integrations_exist,
    get_application_state,
    get_integrations,
    get_unit_info,
    get_unit_state,
    integrations_are_removed,
    units_have_message,
)


@pytest.fixture
def sample_minimal_status() -> jubilant.Status:
    return jubilant.Status(
        model=jubilant.statustypes.ModelStatus(
            name="mdl",
            type="typ",
            controller="ctl",
            cloud="aws",
            version="3.0.0",
        ),
        machines={},
        apps={},
    )


@pytest.fixture
def sample_database_webapp_status() -> jubilant.Status:
    return jubilant.Status._from_dict(
        {
            "model": {
                "name": "tt",
                "type": "caas",
                "controller": "microk8s-localhost",
                "cloud": "microk8s",
                "region": "localhost",
                "version": "3.6.1",
                "model-status": {"current": "available", "since": "24 Feb 2025 12:02:57+13:00"},
                "sla": "unsupported",
            },
            "machines": {},
            "applications": {
                "database": {
                    "charm": "local:database-0",
                    "base": {"name": "ubuntu", "channel": "22.04"},
                    "charm-origin": "local",
                    "charm-name": "database",
                    "charm-rev": 0,
                    "scale": 1,
                    "provider-id": "fa764a56-2b71-4f7e-a6eb-b265f13adc4c",
                    "address": "10.152.183.228",
                    "exposed": False,
                    "application-status": {
                        "current": "active",
                        "message": "relation-created: added new secret",
                        "since": "24 Feb 2025 16:59:43+13:00",
                    },
                    "relations": {
                        "db": [
                            {"related-application": "webapp", "interface": "dbi", "scope": "global"},
                            {"related-application": "dummy", "interface": "xyz", "scope": "foobar"},
                        ]
                    },
                    "units": {
                        "database/0": {
                            "workload-status": {
                                "current": "active",
                                "message": "relation-created: added new secret",
                                "since": "24 Feb 2025 16:59:43+13:00",
                            },
                            "juju-status": {
                                "current": "idle",
                                "since": "24 Feb 2025 16:59:44+13:00",
                                "version": "3.6.1",
                            },
                            "leader": True,
                            "address": "10.1.164.190",
                            "provider-id": "database-0",
                            "open-ports": ["8080/tcp"],
                        }
                    },
                    "endpoint-bindings": {"": "alpha", "db": "alpha"},
                },
                "webapp": {
                    "charm": "local:webapp-0",
                    "base": {"name": "ubuntu", "channel": "22.04"},
                    "charm-origin": "local",
                    "charm-name": "webapp",
                    "charm-rev": 0,
                    "scale": 1,
                    "provider-id": "5c49f9f9-09b3-4212-8a36-dfc081ee80b3",
                    "address": "10.152.183.254",
                    "exposed": False,
                    "application-status": {
                        "current": "active",
                        "message": "relation-changed: would update web app's db secret",
                        "since": "24 Feb 2025 16:59:43+13:00",
                    },
                    "relations": {"db": [{"related-application": "database", "interface": "dbi", "scope": "global"}]},
                    "units": {
                        "webapp/0": {
                            "workload-status": {
                                "current": "active",
                                "message": "relation-changed: would update web app's db secret",
                                "since": "24 Feb 2025 16:59:43+13:00",
                            },
                            "juju-status": {
                                "current": "idle",
                                "since": "24 Feb 2025 16:59:44+13:00",
                                "version": "3.6.1",
                            },
                            "leader": True,
                            "address": "10.1.164.179",
                            "provider-id": "webapp-0",
                        }
                    },
                    "endpoint-bindings": {"": "alpha", "db": "alpha"},
                },
            },
            "storage": {},
            "controller": {"timestamp": "17:00:33+13:00"},
        }
    )


@pytest.fixture
def sample_non_k8s_status() -> jubilant.Status:
    return jubilant.Status._from_dict(
        {
            "model": {
                "name": "tt",
                "type": "iaas",
                "controller": "some-controller",
                "cloud": "aws",
                "region": "us-east-1",
                "version": "3.6.1",
                "model-status": {"current": "available", "since": "24 Feb 2025 12:02:57+13:00"},
                "sla": "unsupported",
            },
            "machines": {},
            "applications": {
                "myapp": {
                    "charm": "local:myapp-0",
                    "base": {"name": "ubuntu", "channel": "22.04"},
                    "charm-origin": "local",
                    "charm-name": "myapp",
                    "charm-rev": 0,
                    "exposed": False,
                    "application-status": {
                        "current": "active",
                        "message": "ready",
                        "since": "24 Feb 2025 16:59:43+13:00",
                    },
                    "units": {
                        "myapp/0": {
                            "workload-status": {
                                "current": "active",
                                "message": "ready",
                                "since": "24 Feb 2025 16:59:43+13:00",
                            },
                            "juju-status": {
                                "current": "idle",
                                "since": "24 Feb 2025 16:59:44+13:00",
                                "version": "3.6.1",
                            },
                            "leader": True,
                            "address": "10.0.0.1",
                        },
                    },
                    "endpoint-bindings": {"": "alpha"},
                },
                "badapp": {
                    "charm": "local:badapp-0",
                    "base": {"name": "ubuntu", "channel": "22.04"},
                    "charm-origin": "local",
                    "charm-name": "badapp",
                    "charm-rev": 0,
                    "exposed": False,
                    "application-status": {
                        "current": "waiting",
                        "message": "installing charm software",
                        "since": "24 Feb 2025 16:59:43+13:00",
                    },
                    "units": {
                        "badapp/0": {
                            "workload-status": {
                                "current": "waiting",
                                "message": "installing charm software",
                                "since": "24 Feb 2025 16:59:43+13:00",
                            },
                            "juju-status": {
                                "current": "installing agent",
                                "since": "24 Feb 2025 16:59:44+13:00",
                                "version": "3.6.1",
                            },
                            "leader": True,
                            "address": "10.0.0.2",
                        },
                    },
                    "endpoint-bindings": {"": "alpha"},
                },
            },
            "storage": {},
            "controller": {"timestamp": "17:00:33+13:00"},
        }
    )


@pytest.fixture
def sample_cmr_consumer_status() -> jubilant.Status:
    return jubilant.Status._from_dict(
        {
            "application-endpoints": {
                "remote-offer": {
                    "application-status": {"current": "active", "since": "30 Apr 2026 07:25:58Z"},
                    "endpoints": {"database": {"interface": "postgresql_client", "role": "provider"}},
                    "relations": {"database": ["pgbouncer-k8s"]},
                    "url": "lxd:admin/model.neighbor-offer",
                }
            },
            "applications": {
                "pgbouncer-k8s": {
                    "address": "10.152.183.229",
                    "application-status": {"current": "active", "since": "30 Apr 2026 07:28:55Z"},
                    "base": {"channel": "22.04", "name": "ubuntu"},
                    "charm": "pgbouncer-k8s",
                    "charm-channel": "1/stable",
                    "charm-name": "pgbouncer-k8s",
                    "charm-origin": "charmhub",
                    "charm-rev": 519,
                    "endpoint-bindings": {
                        "": "alpha",
                        "backend-database": "alpha",
                        "certificates": "alpha",
                        "database": "alpha",
                        "db": "alpha",
                        "db-admin": "alpha",
                        "grafana-dashboard": "alpha",
                        "logging": "alpha",
                        "metrics-endpoint": "alpha",
                        "pgb-peers": "alpha",
                        "tracing": "alpha",
                        "upgrade": "alpha",
                    },
                    "exposed": False,
                    "provider-id": "00f65e2f-269e-4f78-9969-57d6fb8a65c0",
                    "relations": {
                        "backend-database": [{"interface": "postgresql_client", "related-application": "remote-offer"}],
                        "pgb-peers": [
                            {"interface": "pgb_peers", "related-application": "pgbouncer-k8s", "scope": "global"}
                        ],
                        "upgrade": [
                            {"interface": "upgrade", "related-application": "pgbouncer-k8s", "scope": "global"}
                        ],
                    },
                    "scale": 1,
                    "units": {
                        "pgbouncer-k8s/0": {
                            "address": "10.1.0.30",
                            "juju-status": {"current": "idle", "since": "30 Apr 2026 08:20:35Z", "version": "3.6.21"},
                            "leader": True,
                            "provider-id": "pgbouncer-k8s-0",
                            "workload-status": {"current": "active", "since": "30 Apr 2026 07:28:55Z"},
                        }
                    },
                    "version": "1.21.0",
                }
            },
            "controller": {"timestamp": "10:32:48Z"},
            "machines": {},
            "model": {
                "cloud": "k8s",
                "controller": "k8s",
                "model-status": {"current": "available", "since": "30 Apr 2026 07:22:50Z"},
                "name": "model",
                "sla": "unsupported",
                "type": "caas",
                "version": "3.6.21",
            },
            "storage": {},
        }
    )


class TestWaitConditions:
    def test_get_unit_info_leader(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_unit_info(sample_database_webapp_status, "database/leader")

        # THEN
        assert result is not None
        assert result.leader is True

    def test_get_unit_info_by_name(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_unit_info(sample_database_webapp_status, "webapp/0")

        # THEN
        assert result is not None
        assert result.workload_status.current == "active"

    def test_get_unit_info_invalid(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_unit_info(sample_minimal_status, "fake/0")

        # THEN
        assert result is None

    def test_get_integrations(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_integrations(sample_database_webapp_status)

        # THEN - returns both directions of the integration
        assert len(result) == 2
        # Check that both directions exist
        expected_endpoints = {
            frozenset(
                [
                    JujuIntegrationApplication("database", "db"),
                    JujuIntegrationApplication("webapp", "db"),
                ]
            )
        }
        actual_endpoints = {frozenset(integration) for integration in result}
        assert actual_endpoints == expected_endpoints

    def test_get_integrations_includes_integrated_offers(self, sample_cmr_consumer_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_integrations(sample_cmr_consumer_status)

        # THEN - returns both directions of the integration
        assert len(result) >= 2
        # Check that both directions exist
        expected_endpoints = {
            frozenset(
                [
                    JujuIntegrationApplication("pgbouncer-k8s", "backend-database"),
                    JujuIntegrationApplication("remote-offer", "database"),
                ]
            )
        }
        actual_endpoints = {frozenset(integration) for integration in result}
        assert expected_endpoints < actual_endpoints

    def test_get_application_state(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_application_state(sample_database_webapp_status, "database")

        # THEN
        assert result.status == "active"

    def test_get_unit_state(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result = get_unit_state(sample_database_webapp_status, "webapp/0")

        # THEN
        assert result is not None
        assert result.status == "active"
        assert "web app" in result.message

    def test_all_statuses_are_in_compliant(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = all_statuses_are_in(
            sample_database_webapp_status,
            application_statuses={"active"},
            unit_statuses={"active"},
            unit_agent_statuses={"idle"},
        )

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}
        assert wait.noncompliant_units == {}
        assert wait.noncompliant_unit_agents == {}

    def test_all_statuses_are_in_application_not_in_status(
        self, sample_database_webapp_status: jubilant.Status
    ) -> None:
        # GIVEN / WHEN
        result, wait = all_statuses_are_in(
            sample_database_webapp_status, "missing", application_statuses={"active"}, unit_statuses={"active"}
        )

        # THEN
        assert result is False
        assert "missing" in wait.noncompliant_applications

    def test_all_statuses_are_in_noncompliant(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = all_statuses_are_in(
            sample_database_webapp_status, application_statuses={"waiting"}, unit_statuses={"waiting"}
        )

        # THEN
        assert result is False
        assert "webapp" in wait.noncompliant_applications

    def test_applications_are_scaled_compliant(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_scaled(sample_database_webapp_status)

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}
        assert wait.noncompliant_units == {}

    def test_applications_are_scaled_application_not_in_status(
        self, sample_database_webapp_status: jubilant.Status
    ) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_scaled(sample_database_webapp_status, "missing")

        # THEN
        assert result is False
        assert "missing" in wait.noncompliant_applications

    def test_applications_are_scaled_non_k8s_compliant(self, sample_non_k8s_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_scaled(sample_non_k8s_status, "myapp")

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}
        assert wait.noncompliant_units == {}

    def test_applications_are_scaled_non_k8s_noncompliant(self, sample_non_k8s_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_scaled(sample_non_k8s_status, "badapp")

        # THEN
        assert result is False
        assert "badapp" in wait.noncompliant_applications
        assert "badapp/0" in wait.noncompliant_units

    def test_application_is_on_revision_match(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = application_is_on_revision(sample_database_webapp_status, "database", 0)

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}

    def test_application_is_on_revision_mismatch(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = application_is_on_revision(sample_database_webapp_status, "database", 1)

        # THEN
        assert result is False
        assert "database" in wait.noncompliant_applications

    def test_application_is_on_revision_application_not_in_status(
        self, sample_database_webapp_status: jubilant.Status
    ) -> None:
        # GIVEN / WHEN
        result, wait = application_is_on_revision(sample_database_webapp_status, "missing", 0)

        # THEN
        assert result is False
        assert "missing" in wait.noncompliant_applications

    def test_units_have_message_match(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = units_have_message("secret", sample_database_webapp_status, "database/0")

        # THEN
        assert result is True
        assert wait.noncompliant_units == {}

    def test_units_have_message_no_match(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = units_have_message("missing", sample_database_webapp_status, "database/0")

        # THEN
        assert result is False
        assert "database/0" in wait.noncompliant_units

    def test_applications_are_removed_none_removed(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_removed(sample_database_webapp_status, "database")

        # THEN
        assert result is False
        assert "database" in wait.noncompliant_applications

    def test_applications_are_removed_all_removed(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_are_removed(sample_minimal_status, "database")

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}

    def test_integrations_are_removed_not_removed(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN - integration that exists in the status
        integration = (
            JujuIntegrationApplication("database", "db"),
            JujuIntegrationApplication("webapp", "db"),
        )

        # WHEN
        result, wait = integrations_are_removed(sample_database_webapp_status, integration)

        # THEN - The integration still exists, so it's not removed
        assert result is False
        assert len(wait.noncompliant_applications) == 2

    def test_integrations_are_removed_all_removed(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN
        integration = (
            JujuIntegrationApplication("webapp", "db"),
            JujuIntegrationApplication("database", "db"),
        )

        # WHEN
        result, wait = integrations_are_removed(sample_minimal_status, integration)

        # THEN
        assert result is True

    def test_integrations_are_removed_ignores_lingering_saas_proxy(
        self, sample_cmr_consumer_status: jubilant.Status
    ) -> None:
        # GIVEN - the relation itself is gone, but the SAAS proxy ("remote-offer") lingers in the
        # consuming model (e.g. "terminating") while its underlying offer is torn down elsewhere.
        # This must NOT block callers like test_remove_and_restore_integration that re-add the
        # relation while the SAAS proxy and its offer are still alive on purpose.
        integration = (
            JujuIntegrationApplication("pgbouncer-k8s", "some-other-endpoint"),
            JujuIntegrationApplication("remote-offer", "database"),
        )

        # WHEN
        result, wait = integrations_are_removed(sample_cmr_consumer_status, integration)

        # THEN - the relation for this exact pair doesn't exist in the status, so it's "removed"
        assert result is True
        assert wait.noncompliant_applications == {}

    def test_applications_have_no_units_false(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_have_no_units(sample_database_webapp_status)

        # THEN
        assert result is False
        assert "database" in wait.noncompliant_applications
        assert "database/0" in wait.noncompliant_units

    def test_applications_have_no_units_true(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN / WHEN
        result, wait = applications_have_no_units(sample_minimal_status)

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}
        assert wait.noncompliant_units == {}

    def test_bundle_integrations_exist_present(self, sample_database_webapp_status: jubilant.Status) -> None:
        # GIVEN - an integration that exists in the status
        integration = (
            JujuIntegrationApplication("database", "db"),
            JujuIntegrationApplication("webapp", "db"),
        )

        # WHEN
        result, wait = bundle_integrations_exist(sample_database_webapp_status, integration)

        # THEN
        assert result is True
        assert wait.noncompliant_applications == {}

    def test_bundle_integrations_exist_missing(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN - an integration that does NOT exist in the (empty) status
        integration = (
            JujuIntegrationApplication("database", "db"),
            JujuIntegrationApplication("webapp", "db"),
        )

        # WHEN
        result, wait = bundle_integrations_exist(sample_minimal_status, integration)

        # THEN
        assert result is False
        assert len(wait.noncompliant_applications) == 2

    def test_bundle_integrations_exist_app_not_in_status(self, sample_minimal_status: jubilant.Status) -> None:
        # GIVEN - an integration where the apps don't exist in status at all
        integration = (
            JujuIntegrationApplication("ghost-app", "ep"),
            JujuIntegrationApplication("other-ghost", "ep"),
        )

        # WHEN
        result, wait = bundle_integrations_exist(sample_minimal_status, integration)

        # THEN
        assert result is False
        assert wait.noncompliant_applications["ghost-app"] is None
        assert wait.noncompliant_applications["other-ghost"] is None
