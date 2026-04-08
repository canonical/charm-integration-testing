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
